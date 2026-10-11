"""The ``jobhunter-ctr`` wrapper (spec 018 One-off commands, C15).

The generated script is run for real against a fake ``podman`` on PATH that records its argv,
so the test checks what the user's shell would actually execute. No real podman is used.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobhunter.ops import ctr
from jobhunter.ops.proc import Result

FAKE_PODMAN = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a"; done > "$ARGV_OUT"
exit "${FAKE_EXIT:-0}"
"""


@pytest.fixture
def layout(tmp_path) -> ctr.Layout:
    cfg = tmp_path / "cfg dir" / "config.toml"
    cfg.parent.mkdir()
    cfg.write_text("")
    return ctr.Layout(tmp_path / "data", tmp_path / "cache", cfg)


def run_script(tmp_path: Path, script: str, *args: str, exit_code: int = 0):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    podman = bindir / "podman"
    podman.write_text(FAKE_PODMAN)
    podman.chmod(0o755)
    path = ctr.write_script(tmp_path / "jobhunter-ctr", script)
    out = tmp_path / "argv.txt"
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "ARGV_OUT": str(out),
        "FAKE_EXIT": str(exit_code),
        "HOME": str(tmp_path),
    }
    done = subprocess.run(
        [str(path), *args], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    argv = out.read_text().splitlines() if out.exists() else None
    return done, argv


def test_script_runs_the_command_in_a_one_off_container(tmp_path, layout):
    mounts = ctr.present(ctr.ctr_secret_mounts(), ["jobhunter_openrouter", "unrelated"])
    done, argv = run_script(tmp_path, ctr.render_script(layout, mounts), "mail", "status")
    assert done.returncode == 0, done.stderr
    assert argv[:2] == ["run", "-i"]  # no -t: stdin is not a terminal here
    assert argv[-3:] == [ctr.IMAGE, "mail", "status"]
    assert argv == ["run", "-i", *ctr.run_options(layout, mounts), ctr.IMAGE, "mail", "status"]
    assert f"{layout.config_file}:/config/config.toml:ro,z" in argv  # path with a space survives
    secret = argv[argv.index("--secret") + 1]
    assert secret == (
        "jobhunter_openrouter,type=mount,target=/run/secrets/openrouter_api_key,"
        "uid=1000,gid=1000,mode=0400"
    )
    assert argv.count("--secret") == 1
    assert "OPENROUTER_API_KEY_FILE=/run/secrets/openrouter_api_key" in argv
    # bind sources are created owner-only, as the units' ExecStartPre does
    assert stat.S_IMODE(layout.data_dir.stat().st_mode) == 0o700
    assert layout.cache_dir.is_dir()


def test_script_passes_the_exit_status_through(tmp_path, layout):
    done, _ = run_script(tmp_path, ctr.render_script(layout), "db", "migrate", exit_code=3)
    assert done.returncode == 3


def test_script_without_arguments_prints_usage(tmp_path, layout):
    done, argv = run_script(tmp_path, ctr.render_script(layout))
    assert done.returncode == 2 and "usage: jobhunter-ctr" in done.stderr
    assert argv is None


def test_script_without_podman_says_so(tmp_path, layout):
    path = ctr.write_script(tmp_path / "jobhunter-ctr", ctr.render_script(layout))
    bindir = tmp_path / "onlymkdir"
    bindir.mkdir()
    (bindir / "mkdir").symlink_to(shutil.which("mkdir"))
    done = subprocess.run(
        ["/bin/sh", str(path), "version"],
        env={"PATH": str(bindir), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 127
    assert "podman not found" in done.stderr


def test_no_secret_named_unless_it_exists(layout):
    script = ctr.render_script(layout, ctr.present(ctr.ctr_secret_mounts(), []))
    assert "--secret" not in script
    assert "Secrets mounted: none" in script


def test_ctr_column_has_every_secret_in_the_matrix():
    names = [m.secret for m in ctr.ctr_secret_mounts()]
    assert names == [
        "jobhunter_anthropic",
        "jobhunter_openrouter",
        "jobhunter_openai_compat",
        "jobhunter_llama",
        "jobhunter_usajobs_key",
        "jobhunter_usajobs_email",
        "jobhunter_google_client",
        "jobhunter_gmail_token",
    ]
    by = {m.secret: m for m in ctr.ctr_secret_mounts()}
    assert by["jobhunter_google_client"].target == "/run/secrets/google_client_secret.json"
    assert by["jobhunter_google_client"].env == "JOBHUNTER_GOOGLE_CLIENT_SECRETS"
    assert by["jobhunter_gmail_token"].env == "JOBHUNTER_GMAIL_TOKEN_FILE"
    assert by["jobhunter_usajobs_key"].env == "USAJOBS_API_KEY_FILE"
    assert by["jobhunter_usajobs_email"].env == "USAJOBS_EMAIL_FILE"


def test_variable_names_come_from_config():
    settings = SimpleNamespace(
        scoring=SimpleNamespace(
            openrouter=SimpleNamespace(api_key_env="MY_ROUTER_KEY"),
            openai_compat=SimpleNamespace(api_key_env="MY_COMPAT_KEY"),
            local=SimpleNamespace(api_key_env="MY_LLAMA_KEY"),
        )
    )
    by = {m.secret: m.env for m in ctr.ctr_secret_mounts(settings)}
    assert by["jobhunter_openrouter"] == "MY_ROUTER_KEY_FILE"
    assert by["jobhunter_openai_compat"] == "MY_COMPAT_KEY_FILE"
    assert by["jobhunter_llama"] == "MY_LLAMA_KEY_FILE"
    assert by["jobhunter_anthropic"] == "ANTHROPIC_API_KEY_FILE"


def test_write_script_is_executable_and_replaces_only_its_own_file(tmp_path, layout):
    dest = tmp_path / "bin" / "jobhunter-ctr"
    ctr.write_script(dest, ctr.render_script(layout))
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755
    ctr.write_script(dest, ctr.render_script(layout, ctr.ctr_secret_mounts()[:1]))
    assert "jobhunter_anthropic" in dest.read_text()
    foreign = tmp_path / "bin" / "other"
    foreign.write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(ctr.CtrError, match="not written by jobhunter"):
        ctr.write_script(foreign, ctr.render_script(layout))
    assert foreign.read_text() == "#!/bin/sh\necho mine\n"
    assert [p.name for p in dest.parent.iterdir() if p.name.startswith(".")] == []


def test_script_has_valid_shell_syntax(tmp_path, layout):
    path = ctr.write_script(tmp_path / "x", ctr.render_script(layout, ctr.ctr_secret_mounts()))
    assert subprocess.run(["/bin/sh", "-n", str(path)]).returncode == 0
    assert os.access(path, os.X_OK)


class Fake:
    def __init__(self, which: set[str], ok: set[str]) -> None:
        self._which, self._ok, self.calls = which, ok, []

    def which(self, name):
        return f"/usr/bin/{name}" if name in self._which else None

    def run(self, argv, *, capture=True, timeout=None):
        self.calls.append(list(argv))
        return Result(0, "a\nb\n") if argv[0] in self._ok else Result(1, "", "nope")


def test_find_podman_walks_the_host_escape_chain():
    assert ctr.find_podman(Fake({"podman"}, {"podman"})) == ["podman"]
    assert ctr.find_podman(Fake({"podman", "flatpak-spawn"}, {"flatpak-spawn"})) == [
        "flatpak-spawn",
        "--host",
        "podman",
    ]
    # distrobox-host-exec only with host-spawn present (it would sudo-install one otherwise)
    fake = Fake({"distrobox-host-exec"}, {"distrobox-host-exec"})
    with pytest.raises(ctr.CtrError, match="not reachable"):
        ctr.find_podman(fake)
    assert fake.calls == []


def test_secret_names_lists_names_only():
    fake = Fake({"podman"}, {"podman"})
    assert ctr.secret_names(fake, ["podman"]) == ["a", "b"]
    assert fake.calls == [["podman", "secret", "ls", "--format", "{{.Name}}"]]
    with pytest.raises(ctr.CtrError, match="secret ls failed"):
        ctr.secret_names(Fake({"podman"}, set()), ["podman"])
