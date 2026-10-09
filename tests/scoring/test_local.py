"""Local model preset, ``llm status``/``llm bench`` and the setup script (specs/016).

No network and no installs: httpx.MockTransport for servers, stub executables for the script.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from test_scorers import chat_reply, request
from test_screen import (
    add_group,
    conn,  # noqa: F401
    profile,  # noqa: F401
    screen_json,
)
from typer.testing import CliRunner

from jobhunter.cli import app
from jobhunter.config import Local, Scoring
from jobhunter.scoring.bench import run_bench
from jobhunter.scoring.scorers import (
    LocalScorer,
    ScorerError,
    local_server_status,
    privacy_notice,
    scorer_from_string,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-local-llm.sh"
GOOD_QUOTE = "Write Terraform modules"


# ─── Scorer string, URLs, cost, concurrency, notice ─────────────────────────


@pytest.mark.parametrize(
    ("runtime", "base"),
    [("llama.cpp", "http://127.0.0.1:8080"), ("ollama", "http://127.0.0.1:11434/v1")],
)
def test_runtime_picks_base_url(runtime, base):
    scoring = Scoring(local=Local(runtime=runtime))
    s = scorer_from_string("local:gpt-oss-20b", scoring=scoring)
    assert isinstance(s, LocalScorer)
    assert s.config.base_url == base
    assert s.url == base.removesuffix("/v1") + "/v1/chat/completions"
    assert s.name == "local:gpt-oss-20b" and s.model == "gpt-oss-20b"
    assert s.supports_batching is False


def test_model_names_keep_colons_and_base_url_override():
    scoring = Scoring(local=Local(runtime="ollama", base_url="http://127.0.0.1:9999"))
    s = scorer_from_string("local:gpt-oss:20b", scoring=scoring)
    assert s.model == "gpt-oss:20b" and s.url == "http://127.0.0.1:9999/v1/chat/completions"


def test_empty_model_rejected():
    with pytest.raises(ScorerError):
        scorer_from_string("local:")


def test_zero_cost_one_at_a_time_long_timeout_no_key(monkeypatch):
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    s = scorer_from_string("local:m")
    assert s.cost(object()) == 0.0
    assert s.config.max_concurrency == 1
    assert s.client.timeout.read == 900.0
    assert s._headers() == {"Content-Type": "application/json"}  # no Authorization


def test_llama_cpp_asks_for_prompt_caching_ollama_does_not():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=chat_reply("{}"))

    for runtime, expected in (("llama.cpp", True), ("ollama", False)):
        seen.clear()
        client = httpx.Client(transport=httpx.MockTransport(handler))
        scoring = Scoring(local=Local(runtime=runtime))
        scorer_from_string("local:m", scoring=scoring, http_client=client).score_one(request())
        assert ("cache_prompt" in seen) is expected
        assert seen["response_format"]["type"] == "json_schema"


def test_privacy_notice_says_data_stays_here():
    notice = privacy_notice("local:gpt-oss-20b", Scoring())
    assert notice is not None
    assert "stay here" in notice and "nothing leaves the computer" in notice
    assert "http://127.0.0.1:8080" in notice


def test_privacy_notice_warns_when_not_loopback():
    scoring = Scoring(local=Local(base_url="http://gpu-box.example.com:8080"))
    notice = privacy_notice("local:m", scoring)
    assert notice is not None and "NOT this machine" in notice
    assert "nothing leaves" not in notice


# ─── API key ────────────────────────────────────────────────────────────────


def capture_headers(seen: list[dict], status: int = 200) -> httpx.Client:
    def handler(req):
        seen.append({k.lower(): v for k, v in req.headers.items()})
        if status == 200:
            return httpx.Response(200, json=chat_reply("{}"))
        return httpx.Response(status, json={"error": "Invalid API Key"})

    return mock_client(handler)


def test_bearer_header_sent_when_key_env_is_set(monkeypatch):
    monkeypatch.setenv("LLAMA_API_KEY", "k-test-123")
    seen: list[dict] = []
    scorer_from_string("local:m", http_client=capture_headers(seen)).score_one(request())
    assert seen[0]["authorization"] == "Bearer k-test-123"


def test_no_authorization_header_when_key_env_is_absent(monkeypatch):
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    seen: list[dict] = []
    scorer_from_string("local:m", http_client=capture_headers(seen)).score_one(request())
    assert "authorization" not in seen[0]


def test_custom_key_env_name(monkeypatch):
    monkeypatch.setenv("MY_LLAMA_KEY", "other")
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    seen: list[dict] = []
    scorer = scorer_from_string(
        "local:m",
        scoring=Scoring(local=Local(api_key_env="MY_LLAMA_KEY")),
        http_client=capture_headers(seen),
    )
    scorer.score_one(request())
    assert seen[0]["authorization"] == "Bearer other"


def test_401_from_server_says_to_set_the_key(monkeypatch):
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    scorer = scorer_from_string("local:m", http_client=capture_headers([], status=401))
    result = scorer.score_one(request())
    assert result.status == "errored"
    assert "401" in result.detail
    assert "set LLAMA_API_KEY" in result.detail


def test_status_sends_key_and_reports_401(monkeypatch):
    monkeypatch.setenv("LLAMA_API_KEY", "k-test-123")
    seen: list[dict] = []
    st = local_server_status(Local(), client=capture_headers(seen))
    assert seen[0]["authorization"] == "Bearer k-test-123"
    assert st.reachable and st.models == [] and not st.unauthorized

    def denied(req):
        return httpx.Response(401, json={"error": "nope"})

    st = local_server_status(Local(), client=mock_client(denied))
    assert st.unauthorized and st.reachable


def test_status_cli_401_message(monkeypatch, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("")
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    from jobhunter.scoring import scorers

    monkeypatch.setattr(
        scorers,
        "local_server_status",
        lambda config, **k: scorers.ServerStatus(
            "http://127.0.0.1:8080", True, [], "HTTP 401 Unauthorized", unauthorized=True
        ),
    )
    res = CliRunner().invoke(app, ["llm", "status"])
    assert res.exit_code == 1
    assert "HTTP 401" in res.output and "set LLAMA_API_KEY" in res.output


# ─── llm status ─────────────────────────────────────────────────────────────


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_status_lists_models():
    def handler(req):
        assert req.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "gpt-oss-20b"}, {"id": "other"}]})

    st = local_server_status(Local(), client=mock_client(handler))
    assert st.reachable and st.models == ["gpt-oss-20b", "other"]
    assert st.url == "http://127.0.0.1:8080"


def test_status_ollama_url_does_not_double_v1():
    paths = []

    def handler(req):
        paths.append(str(req.url))
        return httpx.Response(200, json={"data": []})

    local_server_status(Local(runtime="ollama"), client=mock_client(handler))
    assert paths == ["http://127.0.0.1:11434/v1/models"]


def test_status_falls_back_to_health():
    def handler(req):
        if req.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    st = local_server_status(Local(), client=mock_client(handler))
    assert st.reachable and st.models == [] and "/health ok" in st.detail


def test_status_unreachable():
    def handler(req):
        raise httpx.ConnectError("refused")

    st = local_server_status(Local(), client=mock_client(handler))
    assert not st.reachable and "refused" in st.detail


def test_status_cli(monkeypatch, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("")
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    from jobhunter.scoring import scorers

    monkeypatch.setattr(
        scorers,
        "local_server_status",
        lambda config, **k: scorers.ServerStatus("http://127.0.0.1:8080", True, ["gpt-oss-20b"]),
    )
    res = CliRunner().invoke(app, ["llm", "status"])
    assert res.exit_code == 0, res.output
    assert "server: up" in res.output and "gpt-oss-20b" in res.output

    monkeypatch.setattr(
        scorers,
        "local_server_status",
        lambda config, **k: scorers.ServerStatus("http://127.0.0.1:8080", False, [], "refused"),
    )
    res = CliRunner().invoke(app, ["llm", "status"])
    assert res.exit_code == 1 and "DOWN" in res.output


# ─── llm bench ──────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def bench_scorer(clock: FakeClock, latencies: list[float], bodies: list[str]):
    calls = iter(zip(latencies, bodies, strict=True))

    def handler(req):
        latency, text = next(calls)
        clock.t += latency
        return httpx.Response(200, json=chat_reply(text))

    return scorer_from_string("local:m", http_client=mock_client(handler))


def table_counts(db) -> dict[str, int]:
    return {
        t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("fit_score", "llm_spend")
    }


def test_bench_reports_and_writes_nothing(conn, profile):  # noqa: F811
    for n in range(3):
        add_group(conn, n, profile)
    clock = FakeClock()
    good = screen_json([GOOD_QUOTE, "Participate in a shared weekly on-call rotation"])
    bad_quote = screen_json([GOOD_QUOTE, "this sentence is not in the posting"])
    scorer = bench_scorer(clock, [20.0, 4.0, 6.0], [good, bad_quote, "not json at all"])
    before = table_counts(conn)

    rep = run_bench(conn, scorer, profile, n=10, clock=clock, night_hours=8)

    assert table_counts(conn) == before == {"fit_score": 0, "llm_spend": 0}
    assert rep.attempted == 3 and rep.errored == 0
    assert rep.first_s == 20.0 and rep.subsequent_s == 5.0
    assert rep.schema_valid == 2 and rep.schema_valid_rate == pytest.approx(2 / 3)
    assert (rep.evidence_verified, rep.evidence_total) == (3, 4)
    assert rep.tokens_per_s == pytest.approx(1200 / 30.0)  # 3 x 400 tokens over 30 s
    assert rep.jobs_per_night == int(8 * 3600 / 5.0)
    out = rep.format()
    for needle in ("first job: 20.0 s", "later jobs: 5.0 s", "tokens/s", "2/3", "3/4", "5760"):
        assert needle in out
    # the connection is writable again afterwards
    conn.execute("CREATE TABLE scratch (x)")


def test_bench_respects_n_and_counts_errors(conn, profile):  # noqa: F811
    for n in range(3):
        add_group(conn, n, profile)
    add_group(conn, 9, profile, passed=False)  # failed prefilter: never benchmarked
    clock = FakeClock()

    def handler(req):
        clock.t += 1.0
        return httpx.Response(500)

    scorer = scorer_from_string("local:m", http_client=mock_client(handler))
    rep = run_bench(conn, scorer, profile, n=2, clock=clock)
    assert rep.attempted == 2 and rep.errored == 2 and rep.schema_valid == 0
    assert rep.evidence_rate is None and rep.tokens_per_s is None


def test_bench_cli_end_to_end_writes_no_rows(tmp_path, monkeypatch, profile):  # noqa: F811
    from jobhunter.config import load_settings
    from jobhunter.core import db
    from jobhunter.scoring import profile as profile_mod

    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "c.toml"
    cfg.write_text("")
    monkeypatch.setenv("JOBHUNTER_CONFIG", str(cfg))
    settings = load_settings()
    c = db.connect(settings.paths.db_path)
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    add_group(c, 1, profile)
    c.commit()
    c.close()
    monkeypatch.setattr(profile_mod, "load_profile", lambda *a, **k: profile)

    body = screen_json([GOOD_QUOTE])
    real = httpx.Client

    def fake_client(*args, **kwargs):
        return real(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=chat_reply(body)))
        )

    monkeypatch.setattr(httpx, "Client", fake_client)
    res = CliRunner().invoke(app, ["llm", "bench", "--scorer", "local:m", "--n", "1"])
    assert res.exit_code == 0, res.output
    assert "stay here" in res.output  # privacy notice (stderr is mixed into output)
    assert "jobs scored: 1 of 1" in res.output and "projection" in res.output
    c = db.connect(settings.paths.db_path)
    assert table_counts(c) == {"fit_score": 0, "llm_spend": 0}
    c.close()


# ─── setup script ───────────────────────────────────────────────────────────

TOOLS = (
    "awk",
    "grep",
    "df",
    "sed",
    "head",
    "tr",
    "cat",
    "dirname",
    "env",
    "cut",
    "tail",
    "od",
    "chmod",
    "touch",
    "python3",
)


CPU_ONLY = "Available devices:\n  BLAS: OpenBLAS (0 MiB, 0 MiB free)\n"
CUDA = "Available devices:\n  CUDA0: NVIDIA GeForce GTX 1650 Ti (4096 MiB, 3800 MiB free)\n"


def make_env(
    tmp_path: Path, present: set[str], devices: str | None = None
) -> tuple[dict[str, str], Path]:
    """A PATH holding only basic tools plus stubs. Installers log a call and fail.

    ``devices`` is what the llama-server stub prints for ``--list-devices``. That call only
    reads, so it is not logged. None means no output and a failing exit, like a broken build.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "invoked.log"
    devices_file = tmp_path / "devices.txt"
    devices_file.write_text(devices or "")
    for tool in TOOLS:
        real = shutil.which(tool)
        assert real, tool
        (bin_dir / tool).symlink_to(real)
    for name in ("llama-server", "ollama", "nvidia-smi", "brew", "curl", "sh"):
        if name == "sh" or name in present or name in ("brew", "curl"):
            stub = bin_dir / name
            if name == "nvidia-smi":
                stub.write_text(
                    f'#!/bin/bash\necho nvidia-smi >> "{log}"\n'
                    'echo "NVIDIA GeForce GTX 1650 Ti, 4096"\n'
                )
            elif name == "llama-server":
                listing = f'  cat "{devices_file}"\n  exit 0\n' if devices else "  exit 1\n"
                stub.write_text(
                    '#!/bin/bash\nif [[ "$1" == "--list-devices" ]]; then\n'
                    f"{listing}fi\n"
                    f'echo "llama-server $@" >> "{log}"\nexit 1\n'
                )
            else:
                stub.write_text(f'#!/bin/bash\necho "{name} $@" >> "{log}"\nexit 1\n')
            stub.chmod(0o755)
    return {"PATH": str(bin_dir), "HOME": str(tmp_path)}, log


def run_script(env, *args, stdin="") -> subprocess.CompletedProcess:
    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(SCRIPT), *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def invoked(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [ln for ln in log.read_text().splitlines() if ln != "nvidia-smi"]


def test_script_dry_run_nothing_present(tmp_path):
    env, log = make_env(tmp_path, set())
    res = run_script(env, "--dry-run")
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "llama-server: not found" in out and "ollama: not found" in out
    assert "nvidia-smi not found" in out
    assert "Recommended model:" in out and "Recommended runtime: llama.cpp" in out
    assert "brew install llama.cpp" in out
    assert "--host 127.0.0.1" in out and "0.0.0.0" not in out.replace("OLLAMA_HOST", "")
    for flag in ("--ctx-size 8192", "--threads 6", "--cache-reuse", "--jinja", "--n-gpu-layers 0"):
        assert flag in out
    assert "[dry-run] would ask" in out
    assert invoked(log) == []


def test_script_dry_run_everything_present(tmp_path):
    env, log = make_env(tmp_path, {"llama-server", "ollama", "nvidia-smi"})
    res = run_script(env, "--dry-run", "--model", "gpt-oss-20b")
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "llama-server: found" in out and "ollama: found" in out
    assert "GTX 1650 Ti, 4096 MiB VRAM" in out
    assert "already installed; no install step" in out
    assert "--n-gpu-layers 99 --n-cpu-moe 99" in out
    assert "about 12-13 GB" in out and "RAM needed" in out
    assert invoked(log) == []


def serve_line(out: str) -> str:
    return next(ln for ln in out.splitlines() if ln.startswith("llama-server -hf"))


def test_script_cpu_only_build_plans_cpu_even_with_nvidia_gpu(tmp_path):
    env, log = make_env(tmp_path, {"llama-server", "nvidia-smi"}, devices=CPU_ONLY)
    res = run_script(env, "--dry-run", "--model", "gpt-oss-20b")
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert "CPU-only build" in out
    assert "this build is CPU-only; a CUDA or Vulkan build would use the GPU" in out
    assert "-DGGML_CUDA=ON" in out and "-DGGML_VULKAN=ON" in out
    line = serve_line(out)
    assert "--n-gpu-layers" not in line and "--threads 6" in line
    assert "--n-gpu-layers" not in out
    assert invoked(log) == []


@pytest.mark.parametrize(
    ("model", "gpu"),
    [
        ("gpt-oss-20b", "--n-gpu-layers 99 --n-cpu-moe 99"),
        ("granite-4.0-h-micro", "--n-gpu-layers 99"),
    ],
)
def test_script_cuda_build_keeps_gpu_plan(tmp_path, model, gpu):
    env, log = make_env(tmp_path, {"llama-server", "nvidia-smi"}, devices=CUDA)
    res = run_script(env, "--dry-run", "--model", model)
    assert res.returncode == 0, res.stderr
    assert "GPU build" in res.stdout and "CPU-only" not in res.stdout
    assert gpu in serve_line(res.stdout)
    assert invoked(log) == []


def test_script_serve_command_has_api_key_and_no_slots(tmp_path):
    env, log = make_env(tmp_path, {"llama-server"}, devices=CUDA)
    res = run_script(env, "--dry-run")
    assert res.returncode == 0, res.stderr
    line = serve_line(res.stdout)
    assert '--api-key "$LLAMA_API_KEY"' in line
    assert "--no-slots" in line
    assert "CORS" in res.stdout and "Local server security" in res.stdout
    assert invoked(log) == []


def test_script_dry_run_never_prints_a_key_and_writes_no_env(tmp_path):
    env, _ = make_env(tmp_path, {"llama-server"}, devices=CUDA)
    res = run_script(env, "--dry-run")
    assert res.returncode == 0, res.stderr
    assert "A dry run writes nothing" in res.stdout
    assert not (tmp_path / ".env").exists()


def test_script_generates_stores_and_never_prints_the_key(tmp_path):
    env, log = make_env(tmp_path, {"llama-server"}, devices=CUDA)
    # key prompt: y; serve prompt: n
    res = run_script(env, stdin="y\nn\n")
    assert res.returncode == 0, res.stderr
    env_file = tmp_path / ".env"
    line = next(ln for ln in env_file.read_text().splitlines() if ln.startswith("LLAMA_API_KEY="))
    key = line.split("=", 1)[1]
    assert len(key) >= 32
    assert (env_file.stat().st_mode & 0o777) == 0o600
    assert key not in res.stdout and key not in res.stderr
    assert "value not shown" in res.stdout
    assert invoked(log) == []


def test_script_declined_key_does_not_start_server(tmp_path):
    env, log = make_env(tmp_path, {"llama-server"}, devices=CUDA)
    res = run_script(env, stdin="n\n")
    assert res.returncode == 0, res.stderr
    assert not (tmp_path / ".env").exists()
    assert "Not starting llama-server" in res.stdout
    assert invoked(log) == []


def test_script_reuses_key_already_in_env_file_without_printing_it(tmp_path):
    (tmp_path / ".env").write_text("LLAMA_API_KEY=SECRET-KEY-VALUE-1234567890abcdef\n")
    env, log = make_env(tmp_path, {"llama-server"}, devices=CUDA)
    res = run_script(env, stdin="n\n")  # serve declined
    assert res.returncode == 0, res.stderr
    assert "LLAMA_API_KEY is set (value not shown)" in res.stdout
    assert "SECRET-KEY" not in res.stdout and "SECRET-KEY" not in res.stderr
    assert (tmp_path / ".env").read_text().count("LLAMA_API_KEY=") == 1
    assert invoked(log) == []


def test_script_ollama_option_prints_official_installer_and_pull(tmp_path):
    env, log = make_env(tmp_path, set())
    res = run_script(env, "--dry-run", "--runtime", "ollama", "--model", "granite-4.0-h-micro")
    assert res.returncode == 0, res.stderr
    assert "curl -fsSL https://ollama.com/install.sh | sh" in res.stdout
    assert "ollama pull granite4:micro-h" in res.stdout
    assert "http://127.0.0.1:11434/v1" in res.stdout
    assert invoked(log) == []


def test_script_answering_no_runs_no_installer(tmp_path):
    env, log = make_env(tmp_path, set())
    res = run_script(env, stdin="n\nn\nn\n")
    assert res.returncode == 0, res.stderr
    assert "Run it? [y/N]" in res.stdout and "skipped." in res.stdout
    assert invoked(log) == []


def test_script_no_stdin_means_no(tmp_path):
    env, log = make_env(tmp_path, set())
    res = run_script(env, stdin="")
    assert res.returncode == 0, res.stderr
    assert invoked(log) == []


def test_script_rejects_bad_arguments(tmp_path):
    env, log = make_env(tmp_path, set())
    assert run_script(env, "--runtime", "vllm").returncode == 2
    assert run_script(env, "--model", "nope").returncode == 2
    assert run_script(env, "--bogus").returncode == 2
    assert invoked(log) == []
    assert os.access(SCRIPT, os.X_OK)
