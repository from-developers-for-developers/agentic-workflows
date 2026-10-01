# SPDX-License-Identifier: GPL-3.0-or-later
"""Compatibility with an extension installed as a separate distribution."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from tests.workflow_helpers import start_after_init
from ww.extensions import ExtensionRegistry
from ww.service import WorkflowService
from ww.storage import Storage


def _build_fixture_wheel(destination: Path) -> Path:
    fixture = Path(__file__).parents[1] / "fixtures" / "packaged_extension"
    wheel = destination / "ww_test_packaged_extension-1.2.3-py3-none-any.whl"
    metadata = "ww_test_packaged_extension-1.2.3.dist-info"
    with ZipFile(wheel, "w", ZIP_DEFLATED) as archive:
        archive.write(
            fixture / "ww_test_packaged_extension.py",
            "ww_test_packaged_extension.py",
        )
        archive.writestr(
            f"{metadata}/METADATA",
            "Metadata-Version: 2.1\nName: ww-test-packaged-extension\nVersion: 1.2.3\n",
        )
        archive.writestr(
            f"{metadata}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: ww-release-gate\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(
            f"{metadata}/entry_points.txt",
            "[ww.extensions]\nacme.packaged = ww_test_packaged_extension:EXTENSION\n",
        )
        archive.writestr(f"{metadata}/RECORD", "")
    return wheel


def test_separately_packaged_extension_is_discovered_and_executed(
    tmp_path: Path, monkeypatch
) -> None:
    site_packages = tmp_path / "site-packages"
    wheel = _build_fixture_wheel(tmp_path)
    installed = subprocess.run(
        (
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-build-isolation",
            "--target",
            str(site_packages),
            str(wheel),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr

    monkeypatch.syspath_prepend(str(site_packages))
    importlib.invalidate_caches()
    (tmp_path / "ww.yaml").write_text(
        """workflows:
  - name: task
    steps:
      - name: work
        description: Do the work.
        hooks:
          after_complete:
            - name: ext/acme/packaged/handlers:record
""",
        encoding="utf-8",
    )

    registry = ExtensionRegistry.discover(tmp_path)
    assert "acme/packaged" in registry.identifiers
    identity = registry.identity("acme/packaged")
    assert identity.version == "1.2.3"
    assert identity.source.startswith("package:ww-test-packaged-extension@1.2.3:")

    service = WorkflowService(Storage(tmp_path), extensions=registry)
    start_after_init(service, "task", "TASK-PACKAGED", agent="codex")
    service.next("TASK-PACKAGED")
    summary = service.complete(
        "TASK-PACKAGED",
        artifact="done",
        summary_for_next="Done.",
    )
    assert summary.item_name == "update-workflow-summary"
    service.next("TASK-PACKAGED")
    result = service.complete(
        "TASK-PACKAGED",
        (("summary", "Packaged extension passed."),),
        summary_for_next="Done.",
    )

    assert result.status == "completed"
    recorded = tmp_path / ".ww/ext/acme/packaged/installed.txt"
    assert recorded.read_text(encoding="utf-8").startswith("TASK-PACKAGED:01-task:")
