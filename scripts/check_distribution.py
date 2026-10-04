# SPDX-License-Identifier: GPL-3.0-or-later

"""Build and validate release artifacts in an environment outside the checkout."""

from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Skills init installs: the setup guide with the project learning it starts.
RETAINED_SKILLS = ("ww-setup", "ww-learn-project", "ww-suggest", "ww-refresh")
# Retired managed assets must not ship again.
RETIRED_ASSETS = ("ww/assets/ww-learn_skill.md",)
REQUIRED_WHEEL_PATHS = {
    "ww/assets/agent_instructions.md",
    "ww/assets/ww_skill.md",
    *(f"ww/assets/{name}_skill.md" for name in RETAINED_SKILLS),
    "ww/assets/workflows/onboarding.yaml",
    "ww/_bundled_extensions/ww/git/extension.py",
}
REQUIRED_SDIST_PATHS = {
    "src/ww/assets/agent_instructions.md",
    "src/ww/assets/ww_skill.md",
    *(f"src/ww/assets/{name}_skill.md" for name in RETAINED_SKILLS),
    "ext/ww/git/extension.py",
}


def _run(*command: str, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _archive_members(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    with tarfile.open(path) as archive:
        return {member.name.split("/", 1)[-1] for member in archive.getmembers()}


def _require_members(path: Path, required: set[str]) -> None:
    members = _archive_members(path)
    missing = sorted(name for name in required if name not in members)
    if missing:
        detail = ", ".join(missing)
        raise RuntimeError(f"{path.name} is missing required files: {detail}")


def _forbid_members(path: Path, forbidden: tuple[str, ...]) -> None:
    members = {name.removeprefix("src/") for name in _archive_members(path)}
    present = sorted(name for name in forbidden if name in members)
    if present:
        raise RuntimeError(f"{path.name} ships retired files: {', '.join(present)}")


def _require_license(path: Path) -> None:
    members = _archive_members(path)
    has_license = any(
        member.endswith("/licenses/LICENSE") or member == "LICENSE"
        for member in members
    )
    if not has_license:
        raise RuntimeError(f"{path.name} is missing the LICENSE file")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ww-distribution-") as temporary:
        temporary_root = Path(temporary)
        dist = temporary_root / "dist"
        _run(sys.executable, "-m", "build", "--outdir", str(dist), cwd=ROOT)

        (wheel,) = dist.glob("*.whl")
        (sdist,) = dist.glob("*.tar.gz")
        _require_members(wheel, REQUIRED_WHEEL_PATHS)
        _require_license(wheel)
        _forbid_members(wheel, RETIRED_ASSETS)
        _require_members(sdist, REQUIRED_SDIST_PATHS)
        _require_license(sdist)
        _forbid_members(sdist, RETIRED_ASSETS)

        environment = temporary_root / "venv"
        _run(sys.executable, "-m", "venv", str(environment), cwd=temporary_root)
        executable = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        python = environment / executable
        _run(
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(wheel),
            cwd=temporary_root,
        )
        _run(
            str(python),
            "-c",
            (
                "from importlib.resources import files\n"
                "from pathlib import Path\n"
                "import ww\n"
                "asset = files('ww.assets').joinpath('agent_instructions.md')\n"
                "assert asset.is_file()\n"
                "assert Path(ww.__file__).parent.joinpath("
                "'_bundled_extensions/ww/git/extension.py').is_file()\n"
            ),
            cwd=temporary_root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
