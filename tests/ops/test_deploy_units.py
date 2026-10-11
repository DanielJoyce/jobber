"""deploy/ Quadlet units, systemd timers and compose.yaml (specs/018, ticket C4).

Parses the files and asserts the spec's properties. The generator test runs
``/usr/libexec/podman/quadlet -dryrun -user`` and ``systemd-analyze --user verify`` when those
binaries exist, and skips otherwise. Nothing here needs podman, the network or real secrets.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest
from quadlet_probe import DROPINS_OK, DROPINS_WHY
from ruamel.yaml import YAML

from jobhunter.ops import preflight, schedule

ROOT = Path(__file__).resolve().parents[2]
QUADLET_DIR = ROOT / "deploy" / "quadlet"
SYSTEMD_DIR = ROOT / "deploy" / "systemd"
GENERATOR = Path("/usr/libexec/podman/quadlet")

ONESHOTS = (
    preflight.BACKUP,
    preflight.RUN,
    preflight.COLLECT,
    preflight.RESCORE,
    preflight.VERIFY,
)


def parse(path: Path) -> dict[str, dict[str, list[str]]]:
    """Unit file -> {section: {key: [values]}} (repeated keys keep every value)."""
    out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    section = ""
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, _, value = line.partition("=")
        out[section][key.strip()].append(value.strip())
    return out


def unit(name: str) -> dict[str, dict[str, list[str]]]:
    return parse(QUADLET_DIR / f"{name}.container")


def one(u, section: str, key: str) -> str:
    values = u[section][key]
    assert len(values) == 1, (section, key, values)
    return values[0]


def test_unit_set_matches_preflight_constants():
    found = sorted(p.name for p in QUADLET_DIR.iterdir())
    assert found == sorted(f"{n}.container" for n in preflight.UNITS)


def test_nothing_but_container_files_under_quadlet_dir():
    assert not list(QUADLET_DIR.glob("*.timer")) and not list(QUADLET_DIR.glob("*.service"))
    assert not list(QUADLET_DIR.glob("*.pod"))  # no pod: podman refuses --userns with --pod


@pytest.mark.parametrize("name", preflight.UNITS)
def test_common_container_properties(name):
    u = unit(name)
    c = u["Container"]
    assert c["Image"] == [preflight.IMAGE] == ["localhost/jobhunter:current"]
    assert c["ContainerName"] == [name]
    assert c["UserNS"] == ["keep-id:uid=1000,gid=1000"]
    assert c["Timezone"] == ["local"]
    assert sorted(c["Volume"]) == [
        "%h/.cache/jobhunter:/cache:z",
        "%h/.config/jobhunter/config.toml:/config/config.toml:ro,z",
        "%h/.local/share/jobhunter:/data:z",
    ]
    # the sources match what preflight checks and labels
    assert f"%h/{preflight.DATA_SOURCE}:/data:z" in c["Volume"]
    assert f"%h/{preflight.CACHE_SOURCE}:/cache:z" in c["Volume"]
    assert f"%h/{preflight.CONFIG_SOURCE}:/config/config.toml:ro,z" in c["Volume"]
    assert not any(":Z" in v for v in c["Volume"])
    assert sorted(c["Environment"]) == [
        "JOBHUNTER_CACHE_DIR=/cache",
        "JOBHUNTER_DATA_DIR=/data",
        "JOBHUNTER_DB_PATH=/data/jobhunter.db",
        "JOBHUNTER_PROFILE_DIR=/data/profile",
        "JOBHUNTER_RESUME_PATH=/data/resume",
    ]
    assert u["Service"]["ExecStartPre"] == [
        "/usr/bin/mkdir -p -m 0700 %h/.local/share/jobhunter %h/.cache/jobhunter"
    ]
    assert u["Unit"]["OnFailure"] == ["jobhunter-ctr-notify@%n.service"]
    assert "AutoUpdate" not in c  # images are built locally, there is no registry to pull


@pytest.mark.parametrize("name", preflight.UNITS)
def test_no_secrets_or_values_in_base_units(name):
    text = (QUADLET_DIR / f"{name}.container").read_text()
    assert not re.search(r"^\s*Secret=", text, re.M)
    assert not re.search(r"(_FILE|API_KEY|TOKEN|PASSWORD)\s*=", text)
    assert not re.search(r"sk-[A-Za-z0-9]", text)
    assert "@" not in re.sub(r"jobhunter-ctr-notify@%n", "", text)  # no email addresses


def test_console_unit():
    u = unit(preflight.CONSOLE)
    c = u["Container"]
    assert c["PublishPort"] == ["127.0.0.1:8808:8808"]
    assert c["Network"] == ["pasta:--host-lo-to-ns-lo"]
    assert c["Exec"] == ["console --host 127.0.0.1 --port 8808"]
    assert c["HealthOnFailure"] == ["kill"]
    health = one(u, "Container", "HealthCmd")
    assert "/healthz" in health and "'Host': '127.0.0.1:8808'" in health
    assert u["Service"]["Restart"] == ["on-failure"]
    assert u["Install"]["WantedBy"] == ["default.target"]
    assert "Type" not in u["Service"]


def test_only_the_console_publishes_a_port_and_only_on_loopback():
    for p in QUADLET_DIR.glob("*.container"):
        ports = parse(p)["Container"].get("PublishPort", [])
        if p.stem == preflight.CONSOLE:
            assert ports == ["127.0.0.1:8808:8808"]
        else:
            assert ports == []


EXEC = {
    preflight.BACKUP: "db backup --auto",
    preflight.RUN: "run --wait 4h",
    preflight.COLLECT: "score --collect-pending --wait 2h",
    preflight.RESCORE: "score --rescore-pending --wait 2h",
    preflight.VERIFY: "sources verify --wait 2h",
}


@pytest.mark.parametrize("name", ONESHOTS)
def test_oneshot_units(name):
    u = unit(name)
    assert u["Service"]["Type"] == ["oneshot"]
    assert "Install" not in u  # only timers (or OnSuccess=) start oneshots
    assert u["Container"]["Exec"] == [EXEC[name]]
    assert "PublishPort" not in u["Container"] and "Restart" not in u["Service"]


def test_long_jobs_wait_for_the_run_lock():
    for name in (preflight.RUN, preflight.COLLECT, preflight.RESCORE, preflight.VERIFY):
        assert "--wait" in one(unit(name), "Container", "Exec")


def test_chaining():
    run = unit(preflight.RUN)["Unit"]
    assert run["Requires"] == [f"{preflight.BACKUP}.service"]
    assert run["After"] == [f"{preflight.BACKUP}.service"]
    assert unit(preflight.COLLECT)["Unit"]["OnSuccess"] == [f"{preflight.RESCORE}.service"]
    # nothing Wants= a oneshot; the only Requires= is run -> backup
    for p in QUADLET_DIR.glob("*.container"):
        u = parse(p)
        assert "Wants" not in u["Unit"]
        if p.stem != preflight.RUN:
            assert "Requires" not in u["Unit"]
        assert "BindsTo" not in u["Unit"]


# --- timers ---------------------------------------------------------------------------------


@pytest.mark.parametrize("job", ["run", "collect", "verify"])
def test_timers_match_host_schedule(job):
    ctr = parse(SYSTEMD_DIR / f"jobhunter-ctr-{job}.timer")
    host = parse(Path(schedule.__file__).parent / "units" / f"jobhunter-{job}.timer")
    for key in ("OnCalendar", "Persistent", "RandomizedDelaySec"):
        assert ctr["Timer"].get(key) == host["Timer"].get(key), key
    assert ctr["Timer"]["Persistent"] == ["true"]
    assert ctr["Timer"]["Unit"] == [f"jobhunter-ctr-{job}.service"]
    assert ctr["Install"]["WantedBy"] == ["timers.target"]
    assert f"jobhunter-ctr-{job}.timer" in preflight.CTR_TIMERS


def test_timer_names_are_the_preflight_ones():
    assert sorted(p.name for p in SYSTEMD_DIR.glob("*.timer")) == sorted(preflight.CTR_TIMERS)


def test_notify_unit_is_a_separate_copy():
    ctr = SYSTEMD_DIR / "jobhunter-ctr-notify@.service"
    host = Path(schedule.__file__).parent / "units" / "jobhunter-notify@.service"
    body = lambda p: [  # noqa: E731
        ln for ln in p.read_text().splitlines() if ln.strip() and not ln.startswith("#")
    ]
    assert body(ctr) == body(host)
    assert "jobhunter-notify@" not in "\n".join(body(ctr)).replace("jobhunter-notify ", "")


# --- compose --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compose():
    return YAML(typ="safe").load((ROOT / "compose.yaml").read_text())


def test_compose_properties(compose):
    assert compose["x-podman"]["in_pod"] is False
    assert set(compose["services"]) == {"console", "run"}
    console, run = compose["services"]["console"], compose["services"]["run"]
    assert console["ports"] == ["127.0.0.1:8808:8808"]
    assert console["network_mode"] == "pasta:--host-lo-to-ns-lo"
    assert console["command"][:5] == ["console", "--host", "127.0.0.1", "--port", "8808"]
    assert "ports" not in run and run["profiles"] == ["manual"]
    for svc in (console, run):
        assert svc["image"] == "localhost/jobhunter:current"
        assert svc["userns_mode"] == "keep-id:uid=1000,gid=1000"
        assert all(":Z" not in v for v in svc["volumes"])
        assert any(v.endswith("/config/config.toml:ro,z") for v in svc["volumes"])
        assert svc["environment"]["JOBHUNTER_DATA_DIR"] == "/data"
        assert not svc.get("privileged")
    assert all(s.get("external") is True for s in compose["secrets"].values())
    # container names differ from the Quadlet ones so both can exist
    names = {s["container_name"] for s in compose["services"].values()}
    assert not names & set(preflight.UNITS)
    text = (ROOT / "compose.yaml").read_text()
    assert not re.search(r"sk-[A-Za-z0-9]", text)


# --- generator ------------------------------------------------------------------------------


def _generate(tmp_path: Path, *, dropin: bool) -> dict[str, str]:
    """Run the generator over a copy of deploy/quadlet; {service name: generated text}."""
    units = tmp_path / "units"
    units.mkdir()
    for p in QUADLET_DIR.glob("*.container"):
        shutil.copy(p, units / p.name)
    if dropin:  # the drop-in preflight would write for the Gmail token (run only)
        spec = next(
            s for s in preflight.secret_matrix(_settings()) if s.secret == "jobhunter_gmail_token"
        )
        path = units / f"{preflight.RUN}.container.d" / f"50-{spec.secret}.conf"
        path.parent.mkdir()
        path.write_text(preflight.dropin_text(spec))
    env = {**os.environ, "QUADLET_UNIT_DIRS": str(units)}
    res = subprocess.run(
        [str(GENERATOR), "-dryrun", "-user"], env=env, capture_output=True, text=True, timeout=60
    )
    if res.returncode != 0 and "unsupported key" in res.stderr + res.stdout:
        pytest.skip("this quadlet generator is older than the keys the units use")
    assert res.returncode == 0, res.stderr + res.stdout
    out = res.stdout
    generated = re.findall(r"^---(.+\.service)---$", out, re.M)
    sections = dict(zip(generated, re.split(r"^---.+---$", out, flags=re.M)[1:], strict=True))
    assert sorted(sections) == sorted(preflight.SERVICES)
    assert "Wants=jobhunter-ctr" not in out
    return sections


@pytest.mark.skipif(not GENERATOR.exists(), reason="podman quadlet generator not installed")
def test_quadlet_dry_run_generates_all_base_units(tmp_path):
    sections = _generate(tmp_path, dropin=False)
    assert "Requires=jobhunter-ctr-backup.service" in sections["jobhunter-ctr-run.service"]
    for name, text in sections.items():
        assert "--secret" not in text and "gmail" not in text.lower(), name
        if name != "jobhunter-console.service":
            assert "[Install]" not in text, name
    console = sections["jobhunter-console.service"]
    assert "--publish 127.0.0.1:8808:8808" in console
    assert "--userns keep-id:uid=1000,gid=1000" in console
    assert "WantedBy=default.target" in console
    assert "run --wait 4h" in sections["jobhunter-ctr-run.service"]


@pytest.mark.skipif(not GENERATOR.exists(), reason="podman quadlet generator not installed")
@pytest.mark.skipif(not DROPINS_OK, reason=DROPINS_WHY)
def test_quadlet_dry_run_applies_a_sample_dropin_to_run_only(tmp_path):
    sections = _generate(tmp_path, dropin=True)
    for name, text in sections.items():
        assert ("gmail_token" in text) == (name == "jobhunter-ctr-run.service"), name
    assert "gmail" not in sections["jobhunter-console.service"].lower()
    run = sections["jobhunter-ctr-run.service"]
    assert "--secret jobhunter_gmail_token,type=mount,target=/run/secrets/gmail_token" in run


def _settings():
    from jobhunter.config import Settings

    return Settings()


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze missing")
def test_timers_pass_systemd_analyze_verify(tmp_path):
    rt = Path("/tmp") / f"jh-{os.getpid()}"
    rt.mkdir(mode=0o700, exist_ok=True)
    try:
        d = tmp_path / "u"
        d.mkdir()
        for p in SYSTEMD_DIR.iterdir():
            shutil.copy(p, d / p.name)
        # verify needs the .service a timer triggers; stand-ins with the same names
        for job in ("run", "collect", "verify"):
            (d / f"jobhunter-ctr-{job}.service").write_text(
                "[Unit]\nDescription=stand-in\n[Service]\nType=oneshot\nExecStart=/bin/true\n"
            )
        env = {**os.environ, "XDG_RUNTIME_DIR": str(rt)}
        res = subprocess.run(
            ["systemd-analyze", "--user", "verify", *map(str, sorted(d.glob("*.timer")))],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        problems = [
            ln
            for ln in (res.stderr + res.stdout).splitlines()
            if "jobhunter-ctr" in ln and "OnFailure" not in ln
        ]
        assert not problems, problems
    finally:
        shutil.rmtree(rt, ignore_errors=True)


def test_compose_secret_modes_are_strings_under_yaml_1_1():
    import yaml  # PyYAML (YAML 1.1) reads an unquoted 0400 as decimal 256

    doc = yaml.safe_load((ROOT / "compose.yaml").read_text())
    modes = [s["mode"] for svc in doc["services"].values() for s in svc["secrets"]]
    assert modes and all(m == "0400" for m in modes)
