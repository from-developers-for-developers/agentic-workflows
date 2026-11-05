# SPDX-License-Identifier: GPL-3.0-or-later
"""Whether a project is ready for the ww version an upgrade installs.

A new major version may remove what the one before it deprecated: a
configuration construct, or a plan construct an open run still holds. The
installed ww cannot read the new version's code, so it judges with its own
lists, and that is enough by rule: every removal ships first as a
deprecation whose ``removed_in`` is set, in a release of the major version
before it. The last release of major version N therefore knows every
removal N + 1 makes. A configuration finding is due for ``target`` when its
migration is removed already or its ``removed_in`` is ``target`` or older,
a prerelease counting as the release it precedes; a run is due when
:func:`ww.open_work.runs_refused_by` lists it.

Only ww's own migrations count: an extension's are judged against the
extension's version, which a ww upgrade does not change.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version

from ww.config import load_configuration
from ww.errors import WwError
from ww.executable import ww_command
from ww.extensions import ExtensionRegistry
from ww.migrations.config import CONFIG_MIGRATIONS, Finding
from ww.migrations.config.project import project_findings
from ww.open_work import RefusedRun, runs_refused_by
from ww.storage import Storage
from ww.workflow_config import WorkflowConfiguration


def crosses_major(current: str, target: str) -> bool:
    """Whether ``target`` is in a later major version than ``current``.

    A version that is not a version number crosses nothing.
    """
    try:
        return Version(target).major > Version(current).major
    except InvalidVersion:
        return False


@dataclass(frozen=True)
class Readiness:
    """What a project still holds that the ww version ``target`` refuses."""

    target: str
    findings: tuple[Finding, ...] = ()
    runs: tuple[RefusedRun, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.findings or self.runs)

    def render(self) -> str:
        """The list of what to fix, and how, for the operator."""
        ww = ww_command()
        lines: list[str] = []
        if self.findings:
            lines += [
                f"Configuration ww {self.target} no longer reads:",
                *(f"- {finding.render()}" for finding in self.findings),
                f"Fix it with `{ww} migrate` (or the ww-migrate workflow).",
            ]
        if self.runs:
            lines += [
                f"Tasks in progress whose saved plan ww {self.target} refuses:",
                *(f"- {run.render()}" for run in self.runs),
                f"Finish them first, or discard them with `{ww} reset <task>`.",
            ]
        return "\n".join(lines) + "\n"


def readiness(storage: Storage, target: str) -> Readiness:
    """What the project at ``storage`` holds that ``target`` no longer reads."""
    return Readiness(
        target, _configuration_findings(storage, target), _refused_runs(storage, target)
    )


def _configuration_findings(storage: Storage, target: str) -> tuple[Finding, ...]:
    """ww's own configuration findings due by ``target``.

    The configuration is loaded to name its rule files and its configured
    projects' settings; when it does not load, which a construct removed
    already causes, the workflow and settings files are still read.
    """
    if not storage.config_path.is_file():
        return ()
    due = {
        migration.id
        for migration in CONFIG_MIGRATIONS
        if migration.removed_in is not None and _not_after(migration.removed_in, target)
    }
    extensions: ExtensionRegistry | None = None
    configuration: WorkflowConfiguration | None = None
    with contextlib.suppress(WwError, OSError):
        extensions = ExtensionRegistry.discover(
            storage.root, storage.project_config_path
        )
        configuration = load_configuration(storage.config_path, extensions)
    try:
        migrations = project_findings(
            storage.config_path,
            storage.project_config_path,
            configuration,
            extensions,
        )
    except (WwError, OSError):
        migrations = project_findings(storage.config_path, storage.project_config_path)
    own = {migration.id for migration in CONFIG_MIGRATIONS}
    return tuple(
        finding
        for migration in migrations
        for finding in migration.findings
        if finding.migration in own
        and (finding.kind == "removed" or finding.migration in due)
    )


def _refused_runs(storage: Storage, target: str) -> tuple[RefusedRun, ...]:
    return runs_refused_by(storage.task_persistence, target)


def _not_after(version: str, target: str) -> bool:
    """Whether ``target`` has the removal of ``version``.

    A prerelease counts as the release it precedes, as
    :meth:`ww.migrations.config.ConfigMigration.status` judges it.
    """
    try:
        return Version(version) <= Version(Version(target).base_version)
    except InvalidVersion:
        return False
