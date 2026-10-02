# SPDX-License-Identifier: GPL-3.0-or-later
"""Compatible PyPI versions and cached notices for packaged installations."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from ww import __version__
from ww.executable import ww_command
from ww.updates import interval_seconds, state_path

PACKAGE_NAME = "ww-agentic-workflows"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
HTTP_TIMEOUT_SECONDS = 2.0
MAX_RESPONSE_BYTES = 2_000_000


@dataclass(frozen=True)
class PackageUpdateNotice:
    installed_version: str
    available_version: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def render(self) -> str:
        return (
            "## A newer ww is available\n\n"
            f"{self.installed_version} → {self.available_version}\n"
            f"Run: `{ww_command()} upgrade`\n\n"
            "This notice is shown once. `ww updates` prints it again.\n\n---\n\n"
        )


def allows_prereleases(installed: str, *, pre: bool = False) -> bool:
    return pre or Version(installed).is_prerelease


def latest_compatible(
    data: dict[str, Any], installed: str, *, pre: bool = False
) -> str | None:
    """Select only usable, non-yanked releases newer than this installation."""
    current = Version(installed)
    releases = data.get("releases")
    if not isinstance(releases, dict):
        return None
    candidates: list[Version] = []
    supported_tags = set(sys_tags())
    for text, files in releases.items():
        try:
            candidate = Version(text)
        except (InvalidVersion, TypeError):
            continue
        if candidate <= current or (
            candidate.is_prerelease and not allows_prereleases(installed, pre=pre)
        ):
            continue
        if not isinstance(files, list):
            continue
        for file in files:
            if not isinstance(file, dict) or file.get("yanked", False):
                continue
            try:
                requirement = file.get("requires_python") or ""
                if not SpecifierSet(requirement).contains(platform.python_version()):
                    continue
                if file.get("packagetype") == "bdist_wheel":
                    _, _, _, wheel_tags = parse_wheel_filename(file["filename"])
                    if not wheel_tags.intersection(supported_tags):
                        continue
                elif file.get("packagetype") != "sdist":
                    continue
            except (InvalidSpecifier, InvalidWheelFilename, KeyError, TypeError):
                continue
            candidates.append(candidate)
            break
    return str(max(candidates)) if candidates else None


def fetch_releases() -> dict[str, Any] | None:
    """Bound the network request; offline or malformed responses stay quiet."""
    try:
        request = Request(PYPI_URL, headers={"User-Agent": f"ww/{__version__}"})
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            return None
        data = json.loads(body)
    except (OSError, URLError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def package_state_path() -> Path:
    # Keep separate installations and stable/prerelease preferences independent.
    key = hashlib.sha256(f"{sys.prefix}:{__version__}".encode()).hexdigest()[:16]
    return state_path().with_name(f"package-updates-{key}.json")


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data))
        temporary.replace(path)
    except OSError:
        return


def last_package_notice() -> PackageUpdateNotice | None:
    data = _load(package_state_path())
    available = data.get("available_version")
    try:
        if isinstance(available, str) and Version(available) > Version(__version__):
            return PackageUpdateNotice(__version__, available)
    except InvalidVersion:
        pass
    return None


def pending_package_notice(*, force: bool = False) -> PackageUpdateNotice | None:
    path = package_state_path()
    data = _load(path)
    now = datetime.now(timezone.utc)
    try:
        checked = datetime.fromisoformat(data["last_checked"])
        due = (now - checked).total_seconds() >= interval_seconds()
    except (KeyError, ValueError, TypeError):
        due = True
    if force or due:
        releases = fetch_releases()
        available = latest_compatible(releases, __version__) if releases else None
        data.update(last_checked=now.isoformat(), available_version=available)
        _save(path, data)
    notice = last_package_notice()
    if notice and (force or data.get("announced_version") != notice.available_version):
        return notice
    return None


def mark_package_announced(notice: PackageUpdateNotice) -> None:
    path = package_state_path()
    data = _load(path)
    data["announced_version"] = notice.available_version
    _save(path, data)
