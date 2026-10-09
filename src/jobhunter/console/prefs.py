"""/prefs logic (specs/014): form <-> profile data, diffs, history, preview and estimates.

The page never calls a model. Free changes are previewed over stored scores; paid changes
show an estimate, and choosing to re-score existing jobs only records a ``rescore_request``
row for a later scoring run.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from jobhunter.core import db, geo
from jobhunter.core.models import Bucket, EmploymentType
from jobhunter.pipeline.locations import load_job_group_locations
from jobhunter.scoring import buckets as bk
from jobhunter.scoring.prefilter import _CREDENTIAL_PATTERNS, evaluate
from jobhunter.scoring.profile import (
    Profile,
    ProfileQuery,
    ProfileValidationError,
    load_profile,
    preferences_mtime_ns,
    profile_data,
    save_profile_changes,
)

Form = Mapping[str, Sequence[str]]
Change = tuple[Any, Any]  # (old, new)

PAID_ROOTS = frozenset({"current_focus", "narrative", "resume_path"})
# Dict paths diffed and written as one value: a single weight cannot change on its own
# (they must sum to 1), so history and revert treat the five as one field.
ATOMIC_PATHS = frozenset({"soft.weights", "hard.home"})
RESUME_PATH = "resume_sha256"  # pseudo field in snapshots: hand edits to the resume file
PREVIEW_DAYS = 30
RECENT_DAYS = 14
FALLBACK_COST_PER_JOB = 0.002
WEIGHT_KEYS = ("skills", "seniority", "domain", "comp", "location")
PERIODS = ("year", "month", "hour")
EMPLOYMENT_CHOICES = tuple(e.value for e in EmploymentType if e is not EmploymentType.unknown)
CREDENTIAL_CHOICES = tuple(sorted(_CREDENTIAL_PATTERNS))
RESCORE_SCOPES = ("none", "recent", "all")
OPEN_BUCKETS = frozenset({Bucket.A, Bucket.B, Bucket.C, Bucket.D, Bucket.E})

REASON_LABELS = {
    "salary_below_floor": "Filtered out by salary",
    "salary_unstated": "Hidden: no stated salary",
    "state_not_allowed": "Filtered out by state",
    "remote_only_violation": "Filtered out: not remote",
    "employment_type_excluded": "Filtered out by employment type",
    "title_excluded": "Filtered out by title",
    "credential_required": "Filtered out: credential you lack",
    "closed": "Closed",
    "overseas": "Overseas",
}


# ─── form parsing ───────────────────────────────────────────────────────────


def parse_form(body: bytes) -> dict[str, list[str]]:
    """``application/x-www-form-urlencoded`` body -> multi-dict (no python-multipart needed)."""
    return parse_qs(body.decode("utf-8"), keep_blank_values=True)


def form_value(form: Form, key: str, default: str = "") -> str:
    values = form.get(key)
    return values[-1] if values else default


def _text(raw: str) -> str | None:
    text = raw.replace("\r\n", "\n").strip()
    return text or None


def _lines(raw: str) -> list[str]:
    return [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n") if ln.strip()]


def state_codes(raw: str) -> list[str]:
    out: list[str] = []
    for part in raw.replace(" ", ",").split(","):
        code = part.strip().upper()
        if code and code not in out:
            out.append(code)
    return out


def _number(form: Form, key: str, errors: dict[str, str], *, label: str) -> float | int | None:
    raw = form_value(form, key).replace(",", "").replace("$", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        errors[key] = f"{label} must be a number"
        return None
    return int(value) if value.is_integer() else value


def format_query(query: Mapping[str, Any]) -> str:
    """One query per line: bare comma-separated keywords, else a flow mapping."""
    fields = {k: v for k, v in query.items() if v not in (None, "", [])}
    keywords = fields.get("keywords", [])
    simple = set(fields) == {"keywords"} and all(
        "," not in kw and not kw.lstrip().startswith("{") for kw in keywords
    )
    return ", ".join(keywords) if simple else json.dumps(fields, ensure_ascii=False)


def parse_queries(raw: str, errors: dict[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, line in enumerate(_lines(raw), start=1):
        if line.startswith("{"):
            try:
                value = YAML(typ="safe").load(line)
            except YAMLError:
                errors["queries"] = f"line {n}: not a valid {{key: value}} mapping"
                continue
            if not isinstance(value, dict):
                errors["queries"] = f"line {n}: not a valid {{key: value}} mapping"
                continue
        else:
            value = {"keywords": [kw.strip() for kw in line.split(",") if kw.strip()]}
        try:
            out.append(ProfileQuery.model_validate(value).model_dump(mode="json"))
        except ValueError as exc:
            errors["queries"] = f"line {n}: {str(exc).splitlines()[-1].strip()}"
    return out


def normalize_weights(
    raw: Mapping[str, float], current: Mapping[str, float]
) -> dict[str, float] | None:
    """Slider values -> weights summing to exactly 1.0, or ``current`` if barely moved."""
    values = {k: max(0.0, float(raw.get(k, 0.0))) for k in WEIGHT_KEYS}
    total = sum(values.values())
    if total <= 0:
        return None
    norm = {k: round(v / total, 3) for k, v in values.items()}
    drift = round(1.0 - sum(norm.values()), 3)
    if drift:
        top = max(norm, key=lambda k: norm[k])
        norm[top] = round(norm[top] + drift, 3)
    if all(abs(norm[k] - float(current.get(k, 0.0))) < 0.005 for k in WEIGHT_KEYS):
        return {k: current[k] for k in WEIGHT_KEYS}
    return norm


def form_to_data(form: Form, base: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Overlay the submitted form on ``base`` (from ``profile_data``). Returns (data, errors).

    Errors are keyed by form field name. Fields the page does not edit are kept from ``base``.
    """
    data: dict[str, Any] = copy.deepcopy(dict(base))
    errors: dict[str, str] = {}
    hard, soft = data["hard"], data["soft"]

    period = form_value(form, "salary_period", "year")
    if period not in PERIODS:
        errors["salary_period"] = "period must be year, month or hour"
        period = "year"
    floor = _number(form, "hard.salary_floor.amount", errors, label="Salary floor")
    target = _number(form, "hard.salary_target.amount", errors, label="Target salary")
    hard["salary_floor"] = None if floor is None else {"amount": floor, "period": period}
    hard["salary_target"] = None if target is None else {"amount": target, "period": period}
    hard["hide_unstated_salary"] = "hard.hide_unstated_salary" in form

    soft["state_ranking"] = state_codes(form_value(form, "state_ranking"))
    hard["states_excluded"] = [
        c
        for c in state_codes(form_value(form, "states_excluded"))
        if c not in soft["state_ranking"]
    ]
    hard["remote_ok"] = "hard.remote_ok" in form
    hard["relocation_ok"] = "hard.relocation_ok" in form
    bonus = _number(form, "soft.remote_bonus", errors, label="Remote bonus")
    soft["remote_bonus"] = int(bonus or 0)

    data["target_titles"] = _lines(form_value(form, "target_titles"))
    hard["title_exclusions"] = _lines(form_value(form, "hard.title_exclusions"))
    hard["employment_types_excluded"] = [
        v for v in form.get("hard.employment_types_excluded", []) if v
    ]
    hard["requires_i_lack"] = [v for v in form.get("hard.requires_i_lack", []) if v]

    raw_weights: dict[str, float] = {}
    for key in WEIGHT_KEYS:
        value = _number(form, f"w.{key}", errors, label=key.capitalize())
        raw_weights[key] = float(value or 0)
    weights = normalize_weights(raw_weights, soft["weights"])
    if weights is None:
        errors["weights"] = "at least one weight must be above zero"
    else:
        soft["weights"] = weights

    overrides = dict(data.get("buckets") or {})
    for key, default in bk.DEFAULT_THRESHOLDS.items():
        value = _number(form, f"buckets.{key}", errors, label=key)
        if value is None:
            overrides.pop(key, None)
        elif value != default or key in overrides:
            overrides[key] = value
    data["buckets"] = overrides

    data["queries"] = parse_queries(form_value(form, "queries"), errors)

    data["current_focus"] = {
        "since": _text(form_value(form, "current_focus.since")),
        "doing": _text(form_value(form, "current_focus.doing")),
        "want_more_of": _lines(form_value(form, "current_focus.want_more_of")),
        "done_with": _lines(form_value(form, "current_focus.done_with")),
    }
    data["narrative"] = {
        key: _text(form_value(form, f"narrative.{key}"))
        for key in ("want", "avoid", "dealbreakers_soft", "context")
    }
    return data, errors


def field_for_error(path: str) -> str:
    """Map a model error path (``soft.weights``, ``queries.0.title``) to its form field."""
    parts = []
    for part in path.split("."):
        if part.isdigit():
            break
        parts.append(part)
    field = ".".join(parts)
    aliases = {
        "soft.state_ranking": "state_ranking",
        "hard.states_excluded": "states_excluded",
        "hard.salary_floor.period": "salary_period",
        "hard.salary_target.period": "salary_period",
        "hard.salary_floor": "hard.salary_floor.amount",
        "hard.salary_target": "hard.salary_target.amount",
        "queries": "queries",
    }
    if field.startswith("soft.weights"):
        return "weights"
    if field.startswith("queries"):
        return "queries"
    return aliases.get(field, field)


def errors_for_form(exc: ProfileValidationError) -> dict[str, str]:
    out: dict[str, str] = {}
    for path, message in exc.errors.items():
        out.setdefault(field_for_error(path), message)
    return out


# ─── form view (what the inputs show) ───────────────────────────────────────


def _num_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def view_from_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Field name -> input value for rendering the form from profile data."""
    hard, soft = data["hard"], data["soft"]
    floor, target = hard.get("salary_floor"), hard.get("salary_target")
    focus = data.get("current_focus") or {}
    narrative = data.get("narrative") or {}
    thresholds = {**bk.DEFAULT_THRESHOLDS, **(data.get("buckets") or {})}
    view: dict[str, Any] = {
        "hard.salary_floor.amount": _num_str((floor or {}).get("amount")),
        "hard.salary_target.amount": _num_str((target or {}).get("amount")),
        "salary_period": (floor or target or {}).get("period") or "year",
        "hard.hide_unstated_salary": bool(hard.get("hide_unstated_salary")),
        "state_ranking": ",".join(soft.get("state_ranking") or []),
        "states_excluded": ",".join(hard.get("states_excluded") or []),
        "hard.remote_ok": bool(hard.get("remote_ok")),
        "soft.remote_bonus": _num_str(soft.get("remote_bonus") or 0),
        "hard.relocation_ok": bool(hard.get("relocation_ok")),
        "target_titles": "\n".join(data.get("target_titles") or []),
        "hard.title_exclusions": "\n".join(hard.get("title_exclusions") or []),
        "hard.employment_types_excluded": list(hard.get("employment_types_excluded") or []),
        "hard.requires_i_lack": list(hard.get("requires_i_lack") or []),
        "queries": "\n".join(format_query(q) for q in data.get("queries") or []),
        "current_focus.since": focus.get("since") or "",
        "current_focus.doing": focus.get("doing") or "",
        "current_focus.want_more_of": "\n".join(focus.get("want_more_of") or []),
        "current_focus.done_with": "\n".join(focus.get("done_with") or []),
    }
    for key in WEIGHT_KEYS:
        view[f"w.{key}"] = str(round(float(soft["weights"][key]) * 100))
    for key in bk.DEFAULT_THRESHOLDS:
        view[f"buckets.{key}"] = _num_str(thresholds[key])
    for key in ("want", "avoid", "dealbreakers_soft", "context"):
        view[f"narrative.{key}"] = narrative.get(key) or ""
    return view


def view_from_form(form: Form, fallback: Mapping[str, Any]) -> dict[str, Any]:
    """Re-render exactly what was submitted (after a failed save), starting from ``fallback``."""
    view = dict(fallback)
    for key, current in fallback.items():
        if isinstance(current, bool):
            view[key] = key in form
        elif isinstance(current, list):
            view[key] = [v for v in form.get(key, []) if v]
        elif key in form:
            view[key] = form_value(form, key)
    return view


# ─── diffs ──────────────────────────────────────────────────────────────────


def diff(before: Any, after: Any, prefix: str = "") -> dict[str, Change]:
    """Leaf-level changes between two plain data trees, as ``{dotted.path: (old, new)}``."""
    if (
        isinstance(after, Mapping)
        and (isinstance(before, Mapping) or before is None)
        and prefix not in ATOMIC_PATHS
    ):
        before = before or {}
        out: dict[str, Change] = {}
        keys = list(before) + [k for k in after if k not in before]
        for key in keys:
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(diff(before.get(key), after.get(key), path))
        return out
    if before == after or (before in (None, [], {}) and after in (None, [], {})):
        return {}
    return {prefix: (before, after)}


def is_paid(path: str) -> bool:
    return path.split(".")[0] in PAID_ROOTS


def get_path(data: Mapping[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


# ─── history (profile_change, profile_snapshot) ─────────────────────────────


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat(timespec="seconds")


def snapshot_data(profile: Profile) -> dict[str, Any]:
    data = profile_data(profile)
    data[RESUME_PATH] = hashlib.sha256(profile.resume_text.encode("utf-8")).hexdigest()
    return data


def save_snapshot(conn: sqlite3.Connection, profile: Profile, now: datetime) -> None:
    conn.execute(
        "INSERT INTO profile_snapshot (id, at, data, filter_version, scoring_version) "
        "VALUES (1, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET at = excluded.at, "
        "data = excluded.data, filter_version = excluded.filter_version, "
        "scoring_version = excluded.scoring_version",
        (
            _iso(now),
            json.dumps(snapshot_data(profile), sort_keys=True),
            profile.filter_version,
            profile.scoring_version,
        ),
    )


def record_changes(
    conn: sqlite3.Connection,
    changes: Mapping[str, Change],
    profile: Profile,
    source: str,
    now: datetime,
) -> list[int]:
    """Append one profile_change row per field. Versions are those after the change."""
    ids: list[int] = []
    for path, (old, new) in changes.items():
        cur = conn.execute(
            "INSERT INTO profile_change (at, field_path, old_value, new_value, filter_version, "
            "scoring_version, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(now),
                path,
                json.dumps(old),
                json.dumps(new),
                profile.filter_version,
                profile.scoring_version,
                source,
            ),
        )
        ids.append(int(cur.lastrowid or 0))
    return ids


def detect_file_edits(
    conn: sqlite3.Connection, profile: Profile, now: datetime | None = None
) -> dict[str, Change]:
    """Log hand edits made outside the UI (``source = 'file'``) since the last snapshot.

    The first call only stores a snapshot: with nothing to compare against, nothing is logged.
    """
    now = now or datetime.now(UTC)
    row = conn.execute("SELECT data FROM profile_snapshot WHERE id = 1").fetchone()
    if row is None:
        save_snapshot(conn, profile, now)
        return {}
    changes = diff(json.loads(row["data"]), snapshot_data(profile))
    if changes:
        with db.transaction(conn):
            record_changes(conn, changes, profile, "file", now)
            save_snapshot(conn, profile, now)
    return changes


def apply_changes(
    conn: sqlite3.Connection,
    profile_dir: Path,
    changes: Mapping[str, Change],
    *,
    expected_mtime_ns: int,
    source: str,
    now: datetime,
    require_resume: bool = True,
) -> Profile:
    """Write the changes to the file (atomic, conflict-checked), then log them."""
    new = save_profile_changes(
        profile_dir,
        {path: new for path, (_, new) in changes.items()},
        expected_mtime_ns=expected_mtime_ns,
        require_resume=require_resume,
    )
    with db.transaction(conn):
        record_changes(conn, changes, new, source, now)
        save_snapshot(conn, new, now)
    return new


def revert_change(
    conn: sqlite3.Connection, profile_dir: Path, change_id: int, now: datetime
) -> Profile:
    """Apply a logged change's old value as a new change (``source = 'revert'``).

    Raises KeyError for an unknown id and ValueError for an entry that cannot be reverted
    from the page (a hand edit to the resume file).
    """
    row = conn.execute(
        "SELECT field_path, old_value FROM profile_change WHERE id = ?", (change_id,)
    ).fetchone()
    if row is None:
        raise KeyError(change_id)
    path = row["field_path"]
    if path == RESUME_PATH:
        raise ValueError("resume edits are reverted by editing the resume file")
    profile = load_profile(profile_dir)
    detect_file_edits(conn, profile, now)
    old_value = json.loads(row["old_value"]) if row["old_value"] is not None else None
    current = get_path(profile_data(profile), path)
    if current == old_value:
        return profile
    return apply_changes(
        conn,
        profile_dir,
        {path: (current, old_value)},
        expected_mtime_ns=preferences_mtime_ns(profile_dir),
        source="revert",
        now=now,
    )


def history(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, at, field_path, old_value, new_value, source FROM profile_change "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "at": r["at"],
            "field_path": r["field_path"],
            "old": _short(r["old_value"]),
            "new": _short(r["new_value"]),
            "source": r["source"],
            "revertable": r["field_path"] != RESUME_PATH,
        }
        for r in rows
    ]


def _short(raw: str | None) -> str:
    return display_value(None if raw is None else json.loads(raw))


# ─── preview (free changes) ─────────────────────────────────────────────────


def prefilter_delta(
    conn: sqlite3.Connection, before: Profile, after: Profile, *, days: int, now: datetime
) -> dict[str, Any]:
    """Prefilter rejections under two profiles over canonical jobs first seen in ``days``."""
    since = (now - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT j.* FROM job_group g JOIN job j ON j.id = g.canonical_job_id "
        "WHERE j.first_seen_at >= ?",
        (since,),
    ).fetchall()
    count_b: Counter[str] = Counter()
    count_a: Counter[str] = Counter()
    rejected_b = rejected_a = 0
    for job in rows:
        locs = load_job_group_locations(conn, job["id"])
        passed_b, reasons_b = evaluate(job, locs, before, now)
        passed_a, reasons_a = evaluate(job, locs, after, now)
        rejected_b += not passed_b
        rejected_a += not passed_a
        count_b.update(reasons_b)
        count_a.update(reasons_a)
    reasons = [
        {
            "key": key,
            "label": REASON_LABELS.get(key, key),
            "before": count_b[key],
            "after": count_a[key],
            "delta": count_a[key] - count_b[key],
        }
        for key in sorted(set(count_b) | set(count_a))
        if count_b[key] != count_a[key]
    ]
    return {
        "total": len(rows),
        "rejected": {"before": rejected_b, "after": rejected_a},
        "reasons": reasons,
    }


def moves_up_into_ab(move: Mapping[str, Any]) -> bool:
    """A preview move that lands in A or B from a lower bucket (B -> A counts)."""
    after, before = move["after"], move["before"]
    return after in ("A", "B") and (before not in ("A", "B") or after == "A")


def display_value(value: Any, limit: int = 80) -> str:
    """Compact text for an old/new value in the preview and history."""
    if value is None or value == [] or value == {}:
        return "—"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def signed(n: int | float) -> str:
    """+3 / -3 / 0, the negative with a real minus sign (U+2212)."""
    if n > 0:
        return f"+{n}"
    if n < 0:
        return f"\N{MINUS SIGN}{abs(n)}"
    return "0"


# ─── estimate (paid changes) ────────────────────────────────────────────────


def cost_per_job(conn: sqlite3.Connection) -> tuple[float, str]:
    """Measured screen cost per job: llm_spend, else fit_score.cost_usd, else a default."""
    row = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0), coalesce(sum(calls), 0) FROM llm_spend "
        "WHERE tier = 'screen' AND calls > 0"
    ).fetchone()
    if row[1] and row[0] > 0:
        return float(row[0]) / float(row[1]), "llm_spend"
    row = conn.execute(
        "SELECT avg(cost_usd), count(*) FROM fit_score WHERE tier = 'screen' AND cost_usd > 0"
    ).fetchone()
    if row[1]:
        return float(row[0]), "fit_score"
    return FALLBACK_COST_PER_JOB, "default"


_OPEN_SCORED_SQL = """
SELECT fs.*, j.id AS job_id, j.salary_min, j.salary_max, j.salary_period, j.salary_stated,
       j.location_scope, coalesce(j.posted_at, j.first_seen_at) AS seen_at
FROM fit_score fs
JOIN job_group g ON g.id = fs.job_group_id
JOIN job j ON j.id = g.canonical_job_id
WHERE (j.closes_at IS NULL OR j.closes_at >= ?)
  AND fs.id = (
    SELECT f2.id FROM fit_score f2 WHERE f2.job_group_id = fs.job_group_id
    ORDER BY (f2.tier = 'deep') DESC, f2.created_at DESC, f2.id DESC LIMIT 1
  )
"""


@dataclass(frozen=True)
class RescoreOption:
    scope: str
    label: str
    jobs: int
    cost_usd: float


def rescore_estimate(conn: sqlite3.Connection, profile: Profile, now: datetime) -> dict[str, Any]:
    """Re-score options for a paid change: none / open A-E in 14 days / all open."""
    cost, source = cost_per_job(conn)
    cutoff = (now - timedelta(days=RECENT_DAYS)).isoformat()
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    rows = cur.execute(_OPEN_SCORED_SQL, (now.isoformat(),)).fetchall()
    recent = 0
    for row in rows:
        if (row["seen_at"] or "") < cutoff:
            continue
        locs = load_job_group_locations(conn, row["job_id"])
        if bk.compute_row(row, row, locs, profile).bucket in OPEN_BUCKETS:
            recent += 1
    options = [
        RescoreOption("none", "No, new jobs only", 0, 0.0),
        RescoreOption(
            "recent",
            f"Open A\N{EN DASH}E jobs from the last {RECENT_DAYS} days",
            recent,
            recent * cost,
        ),
        RescoreOption("all", "All open jobs", len(rows), len(rows) * cost),
    ]
    return {"cost_per_job": cost, "cost_source": source, "options": options}


def record_rescore(
    conn: sqlite3.Connection, scope: str, profile: Profile, now: datetime
) -> int | None:
    """Record a re-score request for a later scoring run. Never calls the API."""
    if scope not in ("recent", "all"):
        return None
    estimate = rescore_estimate(conn, profile, now)
    option = next(o for o in estimate["options"] if o.scope == scope)
    cur = conn.execute(
        "INSERT INTO rescore_request (requested_at, scope, scoring_version, job_count, "
        "cost_per_job_usd, estimated_usd) VALUES (?, ?, ?, ?, ?, ?)",
        (
            _iso(now),
            scope,
            profile.scoring_version,
            option.jobs,
            estimate["cost_per_job"],
            option.cost_usd,
        ),
    )
    return int(cur.lastrowid or 0)


def pending_rescores(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM rescore_request WHERE status = 'pending' ORDER BY id DESC"
    ).fetchall()


# ─── page pieces ────────────────────────────────────────────────────────────


def resume_info(conn: sqlite3.Connection, profile: Profile) -> dict[str, Any]:
    path = profile.resume_file
    modified = None
    if path is not None and path.exists():
        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    row = conn.execute(
        "SELECT scoring_version FROM fit_score ORDER BY created_at DESC, id DESC LIMIT 1"
    ).fetchone()
    last = row["scoring_version"] if row else None
    return {
        "path": str(path) if path else None,
        "modified": modified.strftime("%Y-%m-%d %H:%M UTC") if modified else None,
        "sha256": hashlib.sha256(profile.resume_text.encode("utf-8")).hexdigest()[:12],
        "scored": last is not None,
        "changed": last is not None and last != profile.scoring_version,
    }


def cycle_state(ranking: list[str], excluded: list[str], code: str) -> tuple[list, list]:
    """neutral -> ranked (appended) -> excluded -> neutral."""
    ranking, excluded = list(ranking), list(excluded)
    if code in ranking:
        ranking.remove(code)
        excluded.append(code)
    elif code in excluded:
        excluded.remove(code)
    else:
        ranking.append(code)
    return ranking, excluded


def picker(ranking: Sequence[str], excluded: Sequence[str]) -> dict[str, Any]:
    names = {s.usps: s.name for s in geo.STATES}
    tiles = []
    for code, (row, col) in sorted(geo.TILE_GRID.items(), key=lambda kv: kv[1]):
        if code in ranking:
            state, rank = "ranked", ranking.index(code) + 1
            label = f"{names.get(code, code)}: ranked {rank}"
        elif code in excluded:
            state, rank, label = "excluded", None, f"{names.get(code, code)}: excluded"
        else:
            state, rank, label = "neutral", None, f"{names.get(code, code)}: neutral"
        tiles.append(
            {
                "code": code,
                "row": row + 1,
                "col": col + 1,
                "state": state,
                "rank": rank,
                "label": label,
            }
        )
    return {
        "tiles": tiles,
        "ranking": list(ranking),
        "excluded": list(excluded),
        "rows": max(r for r, _ in geo.TILE_GRID.values()) + 1,
        "cols": max(c for _, c in geo.TILE_GRID.values()) + 1,
    }


# ─── plain-language help for the Buckets section ────────────────────────────

BUCKET_INTRO = (
    "Every scored job lands in one bucket. Rules are checked top to bottom; the first that "
    "matches wins. Numbers are 0-100 scores from the fit model. Changing them is free: "
    "buckets are recomputed instantly, nothing is re-scored."
)
BUCKET_NOTE = "Any stated requirement you don't meet (a blocker) drops a job one bucket."

# (bucket, what it means, threshold keys it uses), in the order the rules are checked.
BUCKET_RULES: list[tuple[str, str, str]] = [
    ("G", "Mismatch (G) if skills score is below g_skills. Hidden from the inbox.", "g_skills"),
    ("G", "Mismatch (G) if the domain fit is below g_domain.", "g_domain"),
    (
        "F",
        "Stale match (F): the job matches your older experience (raw skills at least f_raw) "
        "but not your recent work (recent skills below f_recency). This is the "
        "keyword-search trap.",
        "f_recency, f_raw",
    ),
    ("E", "Downlevel (E): the job is below your level AND pay fit is below e_comp.", "e_comp"),
    (
        "C",
        "Stretch up (C): the job is above your level and your recent skills are at least c_skills.",
        "c_skills",
    ),
    (
        "D",
        "Lateral (D): your skills transfer (at least d_skills) but the domain is unfamiliar "
        "(below d_domain).",
        "d_skills, d_domain",
    ),
    (
        "A",
        "Bullseye (A): overall at least a_overall AND recent skills at least a_recency AND "
        "no blockers. Lower these if A stays empty.",
        "a_overall, a_recency",
    ),
    ("B", "Strong (B): overall at least b_overall.", "b_overall"),
]

_VERDICT_TAIL = "Display only; it doesn't move jobs between buckets."
BUCKET_HELP: dict[str, str] = {
    "g_skills": "Mismatch (G) if skills score is below this. Hidden from the inbox.",
    "g_domain": "Mismatch (G) if the domain fit is below this.",
    "f_recency": "Stale match (F) if recent skills are below this while raw skills are high.",
    "f_raw": (
        "Stale match (F): the job matches your older experience (raw skills at least this) "
        "but not your recent work."
    ),
    "e_comp": "Downlevel (E): the job is below your level AND pay fit is below this.",
    "c_skills": (
        "Stretch up (C): the job is above your level and your recent skills are at least this."
    ),
    "d_skills": "Lateral (D): your skills transfer (at least this) but the domain is unfamiliar.",
    "d_domain": "Lateral (D) if the domain fit is below this while your skills transfer.",
    "b_overall": "Strong (B): overall at least this.",
    "a_overall": (
        "Bullseye (A): overall at least this AND recent skills high enough AND no blockers. "
        "Lower it if A stays empty."
    ),
    "a_recency": (
        "Bullseye (A) also needs recent skills at least this. Lower it if A stays empty."
    ),
    "verdict_strong": f"Job card label: overall at least this reads strong. {_VERDICT_TAIL}",
    "verdict_possible": f"Job card label: overall at least this reads possible. {_VERDICT_TAIL}",
    "verdict_weak": f"Job card label: overall at least this reads weak. {_VERDICT_TAIL}",
    "fallback_mismatch_overall": (
        "Catch-all: a job no rule above matched is Mismatch (G) if its overall score is "
        "below this, otherwise Lateral (D)."
    ),
}
