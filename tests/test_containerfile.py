"""Spec 018 C3: the Containerfile and .containerignore keep personal data and secrets out.

No podman here (and none in the sandbox): these tests parse the files. The image build and
smoke test run in CI (C8).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTAINERFILE = REPO / "Containerfile"
CONTAINERIGNORE = REPO / ".containerignore"

ALLOWED_COPY_SOURCES = {"pyproject.toml", "uv.lock", "README.md", "src/"}
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET", re.IGNORECASE)


def instructions() -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) for each Containerfile instruction, continuations joined."""
    out: list[tuple[str, str]] = []
    pending = ""
    for raw in CONTAINERFILE.read_text().splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.startswith("#"):  # comment inside a continued instruction
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        full = (pending + line).strip()
        pending = ""
        head, _, rest = full.partition(" ")
        out.append((head.upper(), rest.strip()))
    assert not pending
    return out


def test_every_from_is_pinned_by_digest() -> None:
    froms = [args for op, args in instructions() if op == "FROM"]
    assert froms
    for args in froms:
        image = args.split()[0]
        assert re.search(r"@sha256:[0-9a-f]{64}$", image), f"unpinned FROM: {image}"


def test_final_user_is_non_root() -> None:
    users = [args for op, args in instructions() if op == "USER"]
    assert users, "no USER instruction"
    uid = users[-1].split(":")[0]
    assert uid == "1000", f"final USER is {users[-1]}"
    # USER must come after the last FROM, i.e. apply to the final stage.
    ops = [op for op, _ in instructions()]
    last_from = len(ops) - 1 - ops[::-1].index("FROM")
    assert ops.index("USER", last_from) > last_from


def test_no_secret_named_env_or_arg() -> None:
    for op, args in instructions():
        if op not in {"ENV", "ARG"}:
            continue
        # ENV A=1 B=2 or ENV A 1; ARG A[=v]
        names = re.findall(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=", " " + args)
        if not names:
            names = [args.split()[0].split("=")[0]]
        for name in names:
            assert not SECRET_NAME.search(name), f"{op} {name} looks like a secret"


def test_only_allowed_paths_are_copied() -> None:
    copies = 0
    for op, args in instructions():
        assert op != "ADD", "use COPY, never ADD"
        if op != "COPY":
            continue
        parts = [p for p in args.split() if not p.startswith("--")]
        if any(p.startswith("--from=") for p in args.split()):
            continue  # stage-to-stage copy, not from the build context
        sources = parts[:-1]
        for src in sources:
            assert src in ALLOWED_COPY_SOURCES, f"COPY of {src} from the build context"
        copies += 1
    assert copies >= 2


def test_container_mode_and_build_commit() -> None:
    text = "\n".join(f"{op} {args}" for op, args in instructions())
    assert re.search(r"\bJOBHUNTER_CONTAINER=1\b", text)
    assert re.search(r"ARG BUILD_COMMIT=", text)
    assert re.search(r"\bJOBHUNTER_BUILD_COMMIT=\$BUILD_COMMIT\b", text)
    assert re.search(r"org\.opencontainers\.image\.revision=\$BUILD_COMMIT\b", text)
    assert 'ENTRYPOINT ["jobhunter"]' in text
    assert 'CMD ["console"]' in text
    assert "--host 0.0.0.0" not in text  # loopback bind per D11
    assert "HEALTHCHECK" not in text  # the unit's HealthCmd owns it


def test_dependency_installs_are_locked_and_dev_free() -> None:
    syncs = [args for op, args in instructions() if op == "RUN" and "uv sync" in args]
    assert syncs
    for args in syncs:
        assert "--locked" in args and "--no-dev" in args


# --- .containerignore, tested by matching a fixture tree the way the builder does --------------


def _compile(pattern: str) -> re.Pattern[str]:
    """Docker/buildah ignore pattern to regex: ``**/`` spans directories, ``*`` stops at ``/``."""
    out = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        elif c == "[":
            j = pattern.index("]", i)
            out += pattern[i : j + 1]
            i = j + 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile(out + r"\Z")


def _patterns() -> list[re.Pattern[str]]:
    lines = [ln.strip() for ln in CONTAINERIGNORE.read_text().splitlines()]
    return [_compile(ln) for ln in lines if ln and not ln.startswith("#")]


def excluded(path: str) -> bool:
    """A path is excluded when it, or any parent directory, matches a pattern."""
    parts = path.split("/")
    prefixes = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    return any(p.match(prefix) for p in _patterns() for prefix in prefixes)


NEVER_IN_IMAGE = [
    "resume/me.md",
    "profile/preferences.yaml",
    "data/jobhunter.db",
    "data/backups/jobhunter-1.db",
    "config.toml",
    "sub/config.toml",
    "local.local.toml",
    "x/y/config.local.toml",
    ".env",
    ".env.local",
    "deep/dir/.env",
    "deep/dir/.env.production",
    "data.migrated-20261009/jobhunter.db",
    "profile.migrated-20261009/preferences.yaml",
    "resume.migrated-20261009/me.md",
    "gmail_token.json",
    "a/b/gmail_token.json",
    "google_client_secret.json",
    "a/google_client_secret_123.apps.googleusercontent.com.json",
    "cert.pem",
    "a/b/c/key.pem",
    ".git/config",
    ".claude/settings.json",
    ".claude/worktrees/x/src/jobhunter/cli.py",
    "tests/test_cli.py",
    "specs/018-containers.md",
    ".venv/bin/python",
    "src/jobhunter/__pycache__/cli.cpython-313.pyc",
    ".pytest_cache/v/cache",
    ".ruff_cache/CACHEDIR.TAG",
    "src/nested/.env",
    "src/jobhunter/gmail_token.json",
]

MUST_BE_IN_CONTEXT = [
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "Containerfile",
    "src/jobhunter/cli.py",
    "src/jobhunter/console/templates/_packet_docs.html",
    "src/jobhunter/core/migrations/0001_initial.sql",
]


@pytest.mark.parametrize("path", NEVER_IN_IMAGE)
def test_personal_and_secret_paths_are_excluded(path: str) -> None:
    assert excluded(path), f"{path} would be sent to the builder"


@pytest.mark.parametrize("path", MUST_BE_IN_CONTEXT)
def test_needed_paths_are_kept(path: str) -> None:
    assert not excluded(path), f"{path} is needed by the build"


def test_every_real_source_file_is_kept() -> None:
    """A pattern like **/data must not swallow a package directory under src/."""
    for f in (REPO / "src").rglob("*"):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(REPO).as_posix()
        assert not excluded(rel), f"{rel} would be excluded from the image"


def test_gitignored_personal_paths_are_all_covered() -> None:
    """Every personal path .gitignore names (its first block) is also in .containerignore."""
    samples = {
        "resume/": "resume/x",
        "profile/": "profile/x",
        "config.toml": "config.toml",
        "*.local.toml": "a.local.toml",
        "data/": "data/x",
        "data.migrated-*/": "data.migrated-20261009/x",
        "profile.migrated-*/": "profile.migrated-20261009/x",
        "resume.migrated-*/": "resume.migrated-20261009/x",
    }
    ignored = {ln.strip() for ln in (REPO / ".gitignore").read_text().splitlines()}
    for entry, sample in samples.items():
        assert entry in ignored, f"{entry} no longer in .gitignore; update this test"
        assert excluded(sample), f"{entry} is gitignored but not in .containerignore"
        assert excluded(f"nested/{sample}"), f"nested {entry} not covered (needs **/ prefix)"


def test_ignore_patterns_all_have_recursive_prefix() -> None:
    for ln in CONTAINERIGNORE.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            assert ln.startswith("**/"), f"{ln}: patterns anchor at the context root"
