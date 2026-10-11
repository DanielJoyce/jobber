"""Shared probe: does the installed quadlet generator apply ``<unit>.container.d/`` drop-ins?

podman 4.9 (CI) generates base units but ignores drop-ins; podman 5 applies them.
"""

import shutil
import subprocess
from pathlib import Path

from jobhunter.ops import preflight as pf

QUADLET = pf.QUADLET


def _quadlet_applies_dropins() -> tuple[bool, str]:
    """Whether the installed generator applies <unit>.container.d/ drop-ins, by trying it."""
    import tempfile

    if not QUADLET.exists():
        return False, "quadlet generator not installed"
    version = pf.quadlet_version(QUADLET) or "unknown"
    root = Path(tempfile.mkdtemp(prefix="jh-qprobe-", dir="/tmp"))
    try:
        (root / "u").mkdir()
        (root / "u" / "probe.container").write_text("[Container]\nImage=localhost/x:1\n")
        (root / "u" / "probe.container.d").mkdir()
        (root / "u" / "probe.container.d" / "50-x.conf").write_text(
            "[Container]\nEnvironment=JH_PROBE=1\n"
        )
        (root / "r").mkdir(mode=0o700)
        done = subprocess.run(
            [str(QUADLET), "-dryrun", "-user"],
            env={
                "QUADLET_UNIT_DIRS": str(root / "u"),
                "HOME": str(root),
                "XDG_RUNTIME_DIR": str(root / "r"),
                "PATH": "/usr/bin:/bin",
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if "JH_PROBE=1" not in done.stdout:
        return False, f"quadlet {version} does not apply .container.d drop-ins (needs podman 5)"
    return True, ""


DROPINS_OK, DROPINS_WHY = _quadlet_applies_dropins()
