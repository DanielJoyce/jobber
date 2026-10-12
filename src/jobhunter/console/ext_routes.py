"""The extension API, ``/ext/v1/`` (specs/017 phase 1e, "Local API security").

One authenticated door for one client, the paired jobhunter extension. ``ext_refusal`` runs in
the app middleware for every ``/ext/`` path **instead of** ``same_origin_writes``:

- ``Host`` exactly ``127.0.0.1:<port>``, ``localhost:<port>`` or ``[::1]:<port>`` (the port the
  browser connects to), even with ``--allow-remote``, so a DNS-rebinding page is refused;
- ``Origin`` exactly the pinned ``chrome-extension://<id>``;
- POST only (reads too); a preflight is answered only for that origin;
- ``X-Jobhunter-Ext`` at least ``MIN_EXTENSION``, else 426;
- a body over ``MAX_BODY_BYTES`` is 413;
- ``Authorization: Bearer <token>`` checked in constant time against ``extension.json``
  (except ``pair``, which takes the one-time code).

Every route answers within 20 seconds (Chrome stops a service worker whose fetch waits ~30 s):
**Score it** takes its claim synchronously, answers 202, and runs the scorer on a daemon thread
with its own connection; ``capture/fetch`` gives up after 15 seconds.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the route signatures.
import concurrent.futures
import json
import logging
import sqlite3
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from jobhunter import container
from jobhunter.apply import (
    answers,
    capture,
    draft_runs,
    export,
    ext_pairing,
    generator,
    labels,
    packets,
    paste,
)
from jobhunter.apply import score as group_score
from jobhunter.apply.capture_models import (
    API_VERSION,
    MAX_BODY_BYTES,
    AddRequest,
    CaptureRequest,
    DescribeRequest,
    DraftAccepted,
    DraftError,
    DraftEstimateResponse,
    DraftGenerateRequest,
    DraftRequest,
    DraftStatusResponse,
    ErrorResponse,
    EstimateResponse,
    FetchRequest,
    GoneResponse,
    GroupRequest,
    LinkRequest,
    PairRequest,
    PairResponse,
    PrepareResponse,
    ScoreAccepted,
    ScoreRequest,
    StatusResponse,
    VersionRequest,
    VersionResponse,
)
from jobhunter.console import packet_docs
from jobhunter.console.inbox_routes import load_console_profile
from jobhunter.console.work import Restarting, refused
from jobhunter.core import bucketnames
from jobhunter.pipeline import applylink
from jobhunter.pipeline.ats_rules import host_of
from jobhunter.pipeline.board_ids import _INDEED_HOST, _LINKEDIN_HOST
from jobhunter.pipeline.dedupe import CLAIM_TTL, USER_REQUESTED_REASONS
from jobhunter.pipeline.listing import from_iso
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import ScorerError, privacy_notice

logger = logging.getLogger(__name__)

PREFIX = f"/ext/{API_VERSION}/"
MIN_EXTENSION = "0.1.0"
CONSOLE_VERSION = "0.1.0"
FETCH_TIMEOUT_S = 15.0
PREFILTER_LINE = "It skips your prefilter rules, because you chose this posting."
NOTICE_HOSTS = (_LINKEDIN_HOST.pattern, _INDEED_HOST.pattern)
_ALLOW_HEADERS = "Authorization, Content-Type, X-Jobhunter-Ext"
RESTARTING_DRAFT = "jobhunter is restarting; try again in a moment"


def _version(value: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in (value or "").strip().split("."))
    except ValueError:
        return None


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})


def _cors(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST",
        "Access-Control-Allow-Headers": _ALLOW_HEADERS,
        "Access-Control-Expose-Headers": "X-Resume-SHA256",
        "Vary": "Origin",
    }


def ext_refusal(
    method: str,
    path: str,
    headers: Mapping[str, str],
    *,
    port: int,
    token_ok: Callable[[str | None], bool],
) -> Response | None:
    """The response that refuses this ``/ext/`` request, or None to let it through."""

    def refuse(status: int, why: str) -> Response:
        logger.warning("refused %s %s: %s", method, path, why)
        return JSONResponse({"error": why}, status_code=status)

    host = headers.get("host", "")
    if host.lower() not in allowed_hosts(port):
        return refuse(403, f"host {host!r} is not this console's loopback address")
    origin = headers.get("origin")
    if origin != ext_pairing.EXTENSION_ORIGIN:
        return refuse(403, f"origin {origin!r} is not the jobhunter extension")
    if method.upper() == "OPTIONS":
        return Response(status_code=204, headers=_cors(origin))
    if method.upper() != "POST":
        return refuse(405, "the extension API is POST only")
    have = _version(headers.get("x-jobhunter-ext"))
    if have is None or have < (_version(MIN_EXTENSION) or (0,)):
        return refuse(426, f"extension older than {MIN_EXTENSION}: reload it from your checkout")
    try:
        length = int(headers.get("content-length") or 0)
    except ValueError:
        length = MAX_BODY_BYTES + 1
    if length > MAX_BODY_BYTES:
        return refuse(413, "request body over 1 MB")
    if path.rstrip("/") != f"{PREFIX}pair":
        auth = headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token_ok(token.strip() or None):
            return refuse(401, "not paired, or the pairing was revoked: pair again in options")
    return None


class _Error(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(body.get("error", ""))
        self.status = status
        self.body = body


def register(
    app: FastAPI,
    get_profile: Callable[[], Profile],
    now: Callable[[], datetime],
) -> None:
    app.state.ext_score_errors: dict[int, str] = {}
    app.state.ext_score_threads: list[threading.Thread] = []
    # Tests inject a fetcher (url -> paste.Fetched); None fetches through FetchContext.
    app.state.ext_fetcher = None

    def settings() -> Any:
        return app.state.settings

    def pairing_path():
        return ext_pairing.state_path(settings().paths.data_dir)

    async def body_of(request: Request, model: type[BaseModel]) -> Any:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise _Error(413, {"error": "request body over 1 MB"})
        try:
            return model.model_validate_json(raw or b"{}")
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            where = ".".join(str(p) for p in first.get("loc", ()))
            raise _Error(
                422, {"error": f"invalid request: {where} {first.get('msg', '')}"}
            ) from exc

    async def run(request: Request, model: type[BaseModel], fn: Callable[..., Any]) -> Response:
        try:
            req = await body_of(request, model)
            return await run_in_threadpool(fn, req)
        except _Error as exc:
            return JSONResponse(exc.body, status_code=exc.status)

    def with_conn(fn: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = app.state.conn_factory()
        try:
            return fn(conn)
        finally:
            conn.close()

    def ok(model: BaseModel, status: int = 200) -> JSONResponse:
        return JSONResponse(model.model_dump(mode="json"), status_code=status)

    def ext_version(request: Request) -> str | None:
        return (request.headers.get("x-jobhunter-ext") or "")[:32] or None

    def gone(conn: sqlite3.Connection, gid: int, job_id: int | None) -> None:
        if not capture.group_exists(conn, gid):
            cur = capture.current_group(conn, gid, job_id)
            raise _Error(404, GoneResponse(gone=True, current_group=cur).model_dump())

    def profile_or_error() -> Profile:
        profile, error = load_console_profile(settings())
        if profile is None:
            raise _Error(409, {"error": f"Fix the preferences file first: {error}"})
        return profile

    def card_profile() -> Profile:
        try:
            return get_profile()
        except Exception:  # a broken profile must not stop capture
            return Profile()

    # ─── pairing and versions ───────────────────────────────────────────────

    @app.post(f"{PREFIX}pair")
    async def ext_pair(request: Request) -> Response:
        def go(req: PairRequest) -> Response:
            origin = request.headers.get("origin", "")
            ext_id = origin.removeprefix("chrome-extension://")
            try:
                token = ext_pairing.redeem(pairing_path(), req.code, ext_id, now())
            except ext_pairing.PairError as exc:
                raise _Error(403, {"error": str(exc)}) from exc
            return ok(PairResponse(token=token, extension_id=ext_id))

        return await run(request, PairRequest, go)

    @app.post(f"{PREFIX}version")
    async def ext_version_route(request: Request) -> Response:
        def go(_req: VersionRequest) -> Response:
            return ok(
                VersionResponse(
                    api=API_VERSION,
                    console=CONSOLE_VERSION,
                    min_extension=MIN_EXTENSION,
                    tracking_params=capture.tracking_params(),
                    tracking_prefixes=["utm_"],
                    disabled_hosts=list(settings().capture.disabled_hosts),
                    notice_hosts=list(NOTICE_HOSTS),
                )
            )

        return await run(request, VersionRequest, go)

    # ─── capture and its follow-up actions ─────────────────────────────────

    def host_allowed(url: str) -> None:
        """[capture] disabled_hosts, checked here too: the extension's cached list may be
        older than the console's (a config change and restart)."""
        host = host_of(url)
        for d in settings().capture.disabled_hosts:
            h = str(d).lower().lstrip(".")
            if h and (host == h or host.endswith("." + h)):
                raise _Error(
                    422, {"error": f"capture is off for {host} ([capture] disabled_hosts)"}
                )

    def capture_errors(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except capture.CaptureError as exc:
            raise _Error(422, {"error": str(exc)}) from exc
        except capture.Conflict as exc:
            raise _Error(409, {"error": str(exc)}) from exc

    @app.post(f"{PREFIX}capture")
    async def ext_capture(request: Request) -> Response:
        version = ext_version(request)

        def go(req: CaptureRequest) -> Response:
            host_allowed(req.facts.url)

            def inner(conn: sqlite3.Connection) -> Response:
                resp = capture_errors(
                    lambda: capture.capture(
                        conn, req, card_profile(), now=now(), ext_version=version
                    )
                )
                return ok(resp)

            return with_conn(inner)

        return await run(request, CaptureRequest, go)

    @app.post(f"{PREFIX}capture/add")
    async def ext_add(request: Request) -> Response:
        version = ext_version(request)

        def go(req: AddRequest) -> Response:
            host_allowed(req.facts.url)
            return with_conn(
                lambda conn: ok(
                    capture_errors(
                        lambda: capture.add(
                            conn, req, card_profile(), now=now(), ext_version=version
                        )
                    )
                )
            )

        return await run(request, AddRequest, go)

    def default_fetcher(url: str) -> paste.Fetched:
        """1a's Fetch posting text: through FetchContext, so robots.txt decides."""
        injected = getattr(app.state, "ctx_factory", None)
        if injected is not None:
            return paste.fetch_posting(injected, url)
        conn = app.state.conn_factory()
        try:
            return paste.fetch_posting(applylink.default_ctx_factory(settings(), conn), url)
        finally:
            conn.close()

    @app.post(f"{PREFIX}capture/fetch")
    async def ext_fetch(request: Request) -> Response:
        version = ext_version(request)

        def go(req: FetchRequest) -> Response:
            fetcher = app.state.ext_fetcher or default_fetcher

            def timed(url: str) -> paste.Fetched:
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                fut = pool.submit(fetcher, url)
                try:
                    return fut.result(timeout=FETCH_TIMEOUT_S)
                except concurrent.futures.TimeoutError as exc:
                    raise _Error(504, {"error": "slow; select the text instead"}) from exc
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)

            def inner(conn: sqlite3.Connection) -> Response:
                try:
                    return ok(
                        capture_errors(
                            lambda: capture.fetch(conn, req, timed, now=now(), ext_version=version)
                        )
                    )
                except paste.PasteError as exc:
                    raise _Error(422, {"error": str(exc)}) from exc

            return with_conn(inner)

        return await run(request, FetchRequest, go)

    @app.post(PREFIX + "groups/{gid}/describe")
    async def ext_describe(request: Request, gid: int) -> Response:
        version = ext_version(request)

        def go(req: DescribeRequest) -> Response:
            host_allowed(req.facts.url)

            def inner(conn: sqlite3.Connection) -> Response:
                gone(conn, gid, req.job_id)
                return ok(
                    capture_errors(
                        lambda: capture.describe(
                            conn, gid, req, card_profile(), now=now(), ext_version=version
                        )
                    )
                )

            return with_conn(inner)

        return await run(request, DescribeRequest, go)

    @app.post(f"{PREFIX}link")
    async def ext_link(request: Request) -> Response:
        version = ext_version(request)

        def go(req: LinkRequest) -> Response:
            def inner(conn: sqlite3.Connection) -> Response:
                for gid in (req.a, req.b):
                    if gid is not None:
                        gone(conn, gid, None)
                try:
                    answer = capture_errors(
                        lambda: capture.link(conn, req, now=now(), ext_version=version)
                    )
                    # A link can abandon a packet: its named copies go now (019561b (3)).
                    export.sync_abandoned(conn, settings().paths.data_dir)
                    return ok(answer)
                except KeyError as exc:
                    gid = int(exc.args[0]) if exc.args else req.a
                    raise _Error(
                        404, {"gone": True, "current_group": capture.current_group(conn, gid, None)}
                    ) from exc

            return with_conn(inner)

        return await run(request, LinkRequest, go)

    # ─── Score it and Prepare packet ───────────────────────────────────────

    def estimate_response(est: group_score.Estimate, notice: str | None) -> EstimateResponse:
        spend = f"${est.estimated_usd:.4f}"
        label = f"Confirm, spend ≈ {spend}" + (" when the batch is collected" if est.batch else "")
        return EstimateResponse(
            group_id=est.group_id,
            scorer=est.scorer,
            cost_per_job=est.cost_per_job,
            cost_source=est.cost_source,
            estimated_usd=est.estimated_usd,
            remaining_usd=est.remaining_usd,
            batch=est.batch,
            refusal=est.refusal,
            token=est.token,
            notice=notice,
            prefilter_line=PREFILTER_LINE,
            confirm_label=label,
        )

    def build_estimate(conn: sqlite3.Connection, gid: int) -> EstimateResponse:
        profile = profile_or_error()
        scoring = settings().scoring
        est = group_score.estimate(conn, profile, scoring, gid, now())
        notice = None
        try:
            notice = privacy_notice(est.scorer, scoring)
        except ScorerError as exc:
            est.refusal = est.refusal or str(exc)
        return estimate_response(est, notice)

    @app.post(PREFIX + "groups/{gid}/estimate")
    async def ext_estimate(request: Request, gid: int) -> Response:
        def go(req: GroupRequest) -> Response:
            def inner(conn: sqlite3.Connection) -> Response:
                gone(conn, gid, req.job_id)
                return ok(build_estimate(conn, gid))

            return with_conn(inner)

        return await run(request, GroupRequest, go)

    score_answers: dict[str, tuple[int, dict[str, Any]]] = {}
    score_lock = threading.Lock()

    def run_score(claim: group_score.Claim, profile: Profile, token: int) -> None:
        try:
            score_claimed(claim, profile)
        finally:
            app.state.work.leave(token)  # registered by ext_score before the thread started

    def score_claimed(claim: group_score.Claim, profile: Profile) -> None:
        conn = app.state.conn_factory()  # its own connection: never the request's
        errors = app.state.ext_score_errors
        try:
            group_score.run_claimed(
                conn,
                profile,
                settings().scoring,
                claim,
                now=now(),
                scorer_factory=app.state.packet_scorer_factory,
                client_factory=app.state.packet_client_factory,
            )
            errors.pop(claim.group_id, None)
        except group_score.ScoreRefused as exc:
            errors[claim.group_id] = str(exc)
        except Exception as exc:  # recorded for status; the claim was restored
            logger.exception("score group %s failed", claim.group_id)
            errors[claim.group_id] = f"scoring failed: {exc}"
        finally:
            conn.close()

    @app.post(PREFIX + "groups/{gid}/score")
    async def ext_score(request: Request, gid: int) -> Response:
        def go(req: ScoreRequest) -> Response:
            with score_lock:
                if req.action_id in score_answers:
                    status, body = score_answers[req.action_id]
                    return JSONResponse(body, status_code=status)

            def inner(conn: sqlite3.Connection) -> Response:
                gone(conn, gid, req.job_id)
                profile = profile_or_error()
                scoring = settings().scoring
                # The work registry (019 S12b): entered before the claim and the thread, left
                # when the thread ends; refused with 503 once a console restart was accepted.
                try:
                    work = app.state.work.enter(f"an extension Score now (group {gid})")
                except Restarting as exc:
                    return refused(JSONResponse({"error": str(exc)}))
                started = False
                try:
                    try:
                        privacy_notice(scoring.screen_scorer, scoring)
                        claim = group_score.start(
                            conn, profile, scoring, gid, token=req.token, now=now()
                        )
                    except group_score.EstimateChanged as exc:
                        body = ErrorResponse(
                            error=str(exc), estimate=build_estimate(conn, gid)
                        ).model_dump(mode="json")
                        return JSONResponse(body, status_code=409)
                    except (group_score.ScoreRefused, ScorerError) as exc:
                        return JSONResponse({"error": str(exc)}, status_code=409)
                    app.state.ext_score_errors.pop(gid, None)
                    thread = threading.Thread(
                        target=run_score,
                        args=(claim, profile, work),
                        name=f"ext-score-{gid}",
                        daemon=True,
                    )
                    app.state.ext_score_threads.append(thread)
                    thread.start()
                    started = True
                finally:
                    if not started:
                        app.state.work.leave(work)
                body = ScoreAccepted(status="in_progress", group_id=gid).model_dump(mode="json")
                with score_lock:
                    score_answers[req.action_id] = (202, body)
                return JSONResponse(body, status_code=202)

            return with_conn(inner)

        return await run(request, ScoreRequest, go)

    def claim_started(conn: sqlite3.Connection, gid: int) -> datetime | None:
        latest: datetime | None = None
        for r in conn.execute(
            "SELECT p.evaluated_at FROM prefilter_result p JOIN job j ON j.id = p.job_id "
            "WHERE j.job_group_id = ? AND p.reasons = ?",
            (gid, USER_REQUESTED_REASONS),
        ):
            try:
                at = from_iso(r[0])
            except (TypeError, ValueError):
                continue
            latest = at if latest is None or at > latest else latest
        return latest

    @app.post(PREFIX + "groups/{gid}/status")
    async def ext_status(request: Request, gid: int) -> Response:
        def go(req: GroupRequest) -> Response:
            def inner(conn: sqlite3.Connection) -> Response:
                gone(conn, gid, req.job_id)
                if group_score.is_scored(conn, gid):
                    g = capture.card(conn, card_profile(), gid, now())
                    return ok(
                        StatusResponse(
                            group_id=gid,
                            state="scored",
                            message="Scored",
                            bucket=g.bucket if g else None,
                            bucket_name=bucketnames.bucket_name(g.bucket) if g else None,
                            verdict=g.verdict if g else None,
                        )
                    )
                batch = conn.execute(
                    "SELECT b.id FROM score_batch_item i JOIN score_batch b ON b.id = i.batch_id "
                    "WHERE i.job_group_id = ? AND b.collected_at IS NULL LIMIT 1",
                    (gid,),
                ).fetchone()
                if batch is not None:
                    return ok(
                        StatusResponse(
                            group_id=gid,
                            state="submitted",
                            message="Submitted in a batch; scored when it is collected",
                            batch_id=str(batch[0]),
                        )
                    )
                started = claim_started(conn, gid)
                if started is not None:
                    if now() - started < CLAIM_TTL:
                        return ok(
                            StatusResponse(
                                group_id=gid,
                                state="in_progress",
                                message="Scoring",
                                started_at=started.isoformat(),
                            )
                        )
                    return ok(
                        StatusResponse(
                            group_id=gid,
                            state="stopped",
                            message=(
                                "The score did not finish, perhaps because the console "
                                "restarted; you can retry now"
                            ),
                            started_at=started.isoformat(),
                        )
                    )
                if (err := app.state.ext_score_errors.get(gid)) is not None:
                    return ok(StatusResponse(group_id=gid, state="error", message=err))
                return ok(StatusResponse(group_id=gid, state="not_scored", message="Not scored"))

            return with_conn(inner)

        return await run(request, GroupRequest, go)

    @app.post(PREFIX + "groups/{gid}/prepare")
    async def ext_prepare(request: Request, gid: int) -> Response:
        def go(req: GroupRequest) -> Response:
            def inner(conn: sqlite3.Connection) -> Response:
                gone(conn, gid, req.job_id)
                pid = packets.prepare(conn, gid, now())
                p = packets.get_packet(conn, pid)
                return ok(
                    PrepareResponse(
                        packet_id=pid,
                        path=f"/packet/{pid}",
                        needs_text=not (p and p.has_description),
                    )
                )

            return with_conn(inner)

        return await run(request, GroupRequest, go)

    # ─── drafting in the background (specs/022 A7) ─────────────────────────
    #
    # estimate shows the runner line and its token; generate checks that token, enters the
    # work registry, takes the document's claim and its draft_run row (generator.start), and
    # answers 202 with the run on a daemon thread that owns the claim and leaves the registry
    # when it ends; draft-status reads the stored state. The runners are app.state.packet_runner
    # and app.state.packet_api_client_factory (set by packet_routes), so tests replace both.

    app.state.ext_draft_threads: list[threading.Thread] = []
    app.state.ext_draft_live: dict[int, threading.Thread] = {}
    draft_answers: dict[str, tuple[int, dict[str, Any]]] = {}

    def require_packet(conn: sqlite3.Connection, pid: int) -> packets.Packet:
        p = packets.get_packet(conn, pid)
        if p is None:
            raise _Error(
                404,
                DraftError(
                    error="No packet for this job yet: Prepare one first (free)",
                    needs_packet=True,
                ).model_dump(mode="json"),
            )
        return p

    def note_lines(raw: str | None) -> list[str] | None:
        if raw is None:
            return None
        lines = answers.clean_lines(raw)
        if len(raw.strip().splitlines()) > answers.MAX_LINES:
            raise _Error(422, {"error": "employer notes are one to three lines"})
        return lines

    def qkey_of(kind: str, question: str) -> str:
        if kind != "question_draft" or not question.strip():
            return ""
        return labels.question_key(question)

    def draft_estimate(
        conn: sqlite3.Connection, pid: int, req: DraftRequest | DraftGenerateRequest
    ) -> tuple[DraftEstimateResponse, packet_docs.DraftLine, generator.Prepared]:
        """The runner line for this request; raises ApplyRefused for the request's own
        refusals (no posting, numeric question, never-store, letter before resume)."""
        profile = profile_or_error()
        runner = req.runner or settings().apply.runner
        prepared = generator.prepare(
            conn, settings(), profile, pid, req.kind, runner=runner,
            question=req.question, notes=note_lines(req.notes),
        )  # fmt: skip
        return line_response(conn, pid, req, prepared, runner)

    def line_response(
        conn: sqlite3.Connection,
        pid: int,
        req: DraftRequest | DraftGenerateRequest,
        prepared: generator.Prepared,
        runner: str,
    ) -> tuple[DraftEstimateResponse, packet_docs.DraftLine, generator.Prepared]:
        line = packet_docs.draft_line(
            conn, settings(), req.kind, prepared.est, packet_id=pid, runner=runner,
            now=now(), cli=app.state.packet_runner,
        )  # fmt: skip
        resp = DraftEstimateResponse(
            packet_id=pid,
            kind=req.kind,
            question_key=prepared.qkey,
            runner=line.runner,
            model=line.model,
            line=line.line,
            notices=line.notices,
            estimated_usd=line.estimated_usd,
            estimate_source=line.estimate_source,
            charged=line.charged,
            cap_usd=line.cap_usd,
            cap_left_usd=line.cap_left_usd,
            confirm_label=line.confirm_label,
            refusal=line.refusal,
            help=line.help,
            token=line.token(pid, req.kind, prepared.qkey),
        )
        return resp, line, prepared

    def refused_draft(exc: generator.ApplyRefused) -> _Error:
        return _Error(409, DraftError(error=exc.message).model_dump(mode="json"))

    @app.post(PREFIX + "packets/{pid}/estimate")
    async def ext_draft_estimate(request: Request, pid: int) -> Response:
        def go(req: DraftRequest) -> Response:
            def inner(conn: sqlite3.Connection) -> Response:
                require_packet(conn, pid)
                try:
                    return ok(draft_estimate(conn, pid, req)[0])
                except generator.ApplyRefused as exc:
                    raise refused_draft(exc) from exc

            return with_conn(inner)

        return await run(request, DraftRequest, go)

    def run_draft(started: generator.Started, work: int) -> None:
        conn = None
        try:
            conn = app.state.conn_factory()  # its own connection: never the request's
            generator.run_claimed(
                conn,
                settings(),
                started,
                client_factory=app.state.packet_api_client_factory,
                cli=app.state.packet_runner,
            )
            # The new version is current: the employer-named attach files must follow it, as
            # the packet page's redirect does, or a superseded version stays attachable.
            warn = export.safe_sync(conn, settings().paths.data_dir, started.packet_id)
            if warn:
                logger.warning("draft for packet %s: %s", started.packet_id, warn)
        except (generator.ApplyRefused, generator.GenerateFailed):
            pass  # recorded on the draft_run row for draft-status
        except Exception:
            logger.exception("draft for packet %s failed", started.packet_id)
        finally:
            started.claim.release()  # idempotent: run_claimed released it unless it never ran
            if conn is not None:
                conn.close()
            app.state.ext_draft_live.pop(started.run_id, None)
            app.state.work.leave(work)  # entered by ext_draft_generate before the claim

    @app.post(PREFIX + "packets/{pid}/generate")
    async def ext_draft_generate(request: Request, pid: int) -> Response:
        def go(req: DraftGenerateRequest) -> Response:
            with score_lock:
                if req.action_id in draft_answers:
                    status, body = draft_answers[req.action_id]
                    return JSONResponse(body, status_code=status)

            def inner(conn: sqlite3.Connection) -> Response:
                require_packet(conn, pid)
                if container.is_container():
                    cmd = packet_docs.host_draft_command(pid, req.kind)
                    raise _Error(409, {"error": f"Drafting runs on the host: `{cmd}`"})
                runner = req.runner or settings().apply.runner
                notes = note_lines(req.notes)
                # The work registry (019 S12b): entered before the claim and the draft_run
                # row, left when the thread ends; 503 with nothing written while restarting.
                try:
                    work = app.state.work.enter(f"an extension draft (packet {pid})")
                except Restarting:
                    return refused(JSONResponse({"error": RESTARTING_DRAFT}))
                thread_started = False
                try:
                    profile = profile_or_error()
                    seen: dict[str, DraftEstimateResponse] = {}

                    def check(prepared: generator.Prepared) -> bool:
                        """The click's token must name this line; spend needs its confirm.
                        Returns whether the confirm stands: only for a charged line."""
                        resp, line, _ = line_response(conn, pid, req, prepared, runner)
                        seen["estimate"] = resp
                        if line.refusal is not None:
                            raise generator.ApplyRefused(line.refusal)
                        if req.token != resp.token:
                            raise generator.EstimateChanged(
                                "The estimate changed since you saw it: check it and press "
                                "Generate again. Nothing was run"
                            )
                        if line.charged and not req.confirm_paid:
                            raise generator.ApplyRefused(
                                f"This draft is charged (≈ ${line.estimated_usd:.2f}): confirm "
                                "the spend first. Nothing was run"
                            )
                        return req.confirm_paid and line.charged

                    try:
                        started = generator.start(
                            conn, settings(), profile, pid, req.kind, runner=runner, now=now(),
                            question=req.question, notes=notes,
                            confirm_paid=req.confirm_paid, check=check,
                            cli=app.state.packet_runner,
                        )  # fmt: skip
                    except generator.ApplyRefused as exc:
                        body = DraftError(error=exc.message, estimate=seen.get("estimate"))
                        return JSONResponse(body.model_dump(mode="json"), status_code=409)
                    try:
                        if notes is not None and req.kind == "cover_letter":
                            # Notes typed beside the letter go with it (as the packet page).
                            answers.save_packet_answer(
                                conn, pid, answers.NOTES_KEY, "Employer notes", "\n".join(notes)
                            )
                        thread = threading.Thread(
                            target=run_draft,
                            args=(started, work),
                            name=f"ext-draft-{pid}-{started.run_id}",
                            daemon=True,
                        )
                        app.state.ext_draft_live[started.run_id] = thread
                        thread.start()
                        thread_started = True
                        # Only a started thread: the shutdown joins every listed one.
                        app.state.ext_draft_threads.append(thread)
                    finally:
                        if not thread_started:
                            app.state.ext_draft_live.pop(started.run_id, None)
                            started.claim.release()  # first: the row write below may fail
                            draft_runs.finish(conn, started.run_id, now(), error="not started")
                finally:
                    if not thread_started:
                        app.state.work.leave(work)
                body = DraftAccepted(
                    status="drafting",
                    packet_id=pid,
                    kind=req.kind,
                    question_key=started.prepared.qkey,
                    run_id=started.run_id,
                    started_at=started.now.isoformat(),
                ).model_dump(mode="json")
                with score_lock:
                    draft_answers[req.action_id] = (202, body)
                return JSONResponse(body, status_code=202)

            try:
                return with_conn(inner)
            except answers.NeverStore as exc:
                raise _Error(409, {"error": str(exc)}) from exc

        return await run(request, DraftGenerateRequest, go)

    def live_run(run_id: int) -> bool:
        t = app.state.ext_draft_live.get(run_id)
        return t is not None and t.is_alive()

    @app.post(PREFIX + "packets/{pid}/draft-status")
    async def ext_draft_status(request: Request, pid: int) -> Response:
        def go(req: DraftRequest) -> Response:
            def inner(conn: sqlite3.Connection) -> Response:
                require_packet(conn, pid)
                qkey = qkey_of(req.kind, req.question)
                data_dir = settings().paths.data_dir
                r = draft_runs.latest(conn, data_dir, pid, req.kind, qkey, live=live_run)
                base = {"packet_id": pid, "kind": req.kind, "question_key": qkey}
                if r is None:
                    return ok(DraftStatusResponse(**base, state="none", message="Not drafted"))
                base.update(
                    run_id=r.id, runner=r.runner, started_at=r.started_at,
                    finished_at=r.finished_at,
                )  # fmt: skip
                sub = "on your subscription" if r.runner == "cli" else "on the API"
                if r.state == draft_runs.DRAFTING:
                    msg = f"Drafting {sub}"
                    return ok(DraftStatusResponse(**base, state="drafting", message=msg))
                if r.state == draft_runs.STOPPED:
                    msg = draft_runs.stopped_message(r.runner)
                    return ok(DraftStatusResponse(**base, state="stopped", message=msg))
                if r.state == draft_runs.FAILED:
                    offer, usd = r.runner == "cli", None
                    if offer:
                        api_est = api_offer(conn, pid, req)
                        offer, usd = api_est is not None, api_est
                    failed = DraftStatusResponse(
                        **base, state="failed", message=r.error or "The draft failed",
                        offer_api=offer, api_estimated_usd=usd,
                    )  # fmt: skip
                    return ok(failed)
                return ok(DraftStatusResponse(**base, state="done", **done_summary(conn, r)))

            return with_conn(inner)

        return await run(request, DraftRequest, go)

    def api_offer(conn: sqlite3.Connection, pid: int, req: DraftRequest) -> float | None:
        """**Run on the API** after a subscription failure: its estimate, when it can run."""
        api_req = req.model_copy(update={"runner": "api"})
        try:
            resp = draft_estimate(conn, pid, api_req)[0]
        except (generator.ApplyRefused, _Error):
            return None
        return None if resp.refusal else resp.estimated_usd

    def done_summary(conn: sqlite3.Connection, r: draft_runs.Run) -> dict[str, Any]:
        row = conn.execute(
            "SELECT version, check_report FROM packet_document WHERE id = ?", (r.doc_id,)
        ).fetchone()
        if row is None:
            return {"message": "Drafted", "doc_id": r.doc_id}
        report = json.loads(row["check_report"] or "{}")
        items = report.get("items") or []
        ent = report.get("entailment") or {}
        return {
            "message": f"v{row['version']} drafted",
            "doc_id": r.doc_id,
            "version": row["version"],
            "check_ok": bool(report.get("ok")),
            "unsupported": sum(1 for i in items if i.get("status") == "unsupported"),
            "entailment": ent.get("status"),
            "entail_no": sum(1 for i in items if i.get("entail") == "no"),
            "entail_partly": sum(1 for i in items if i.get("entail") == "partly"),
        }
