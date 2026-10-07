# SPDX-License-Identifier: GPL-3.0-or-later
"""Git support for ww, as an extension.

This is the first ww extension, and it deliberately lives in ``<root>/ext/``
rather than inside the ww package: it reaches ww only through
``ww.extensions.api``, so it exercises exactly the path a third-party extension
takes.

What it adds over the equivalent shell handlers is memory and configuration. It
records every commit it makes and every branch it opens, and it reads its
settings from the ``ww/git`` section of ``ww.json``:

```json
"extensions": {
  "ww/git": {
    "commit_format": "{{ww.task.id}}: {{commit_message}}",
    "base_branches": {
      "default": "main",
      "bugfix": "develop",
      "task": {"argv": ["./scripts/base-branch", "{{ww.task.lane}}"]}
    },
    "separate_branch": true,
    "branch_name_formats": {
      "default": "feature/{{ww.task.id}}",
      "bugfix": "hotfix/{{ww.task.id}}"
    },
    "worktrees": false
  }
}
```

Handlers adopt state that already satisfies their request where possible and
refuse destructive recovery: nothing here passes ``--force``, discards a
change, or moves work you did not ask it to move. This is not a general
exactly-once guarantee. ``git-commit`` and ``merge-branch`` use ww's stable
operation ID and their checkers to recognize a commit made by an interrupted
attempt before retrying. ``merge-branch`` runs ``git merge --abort`` only on a
merge it started itself from a clean workspace: after a conflict, after a
refused signature, or when an interrupted attempt of the same operation left
it in progress.
Rendered task branch names are normalized to lowercase before Git operations.

A note on worktrees
-------------------

Under ``worktrees: true``, ``start-task-branch`` creates the branch and the
separate ``create-worktree`` handler selects or creates its checkout. ww
persists that directory, shows it to the agent, and runs later automatic
handlers there. The primary checkout is the task workspace when its current
branch exactly matches the task branch rendered from ``branch_name_formats``;
otherwise the separate checkout is used. Before committing, ``git-commit``
verifies that the workspace is the selected Git checkout root and that every
changed and staged path resolves beneath it.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ww.errors import ConfigurationError
from ww.extensions.api import (
    Extension,
    ExtensionCheckResult,
    ExtensionCommand,
    ExtensionContext,
    ExtensionHandler,
    ExtensionNamespace,
    ExtensionResult,
    ExtensionVariable,
    ModeDefinition,
    ProvidedVariable,
)
from ww.interpolation import dependencies, interpolate
from ww.variables import BRANCH_NAMING_STRATEGY, TASK_ID, TASK_WORKSPACE_DIR

COMMITS_FILE = "commits.jsonl"
BRANCHES_FILE = "branches.jsonl"
# ``git status --porcelain`` prefixes each path with two status letters and a space.
_PORCELAIN_STATUS_WIDTH = 3

# The tokens a format reads: the task's ID, its workflow, and its run.
WORKFLOW_TOKEN = "ww.task.workflow"
# The workflow whose branch handling the task takes: its ``hooks_from``, else
# the workflow itself, the key ``branch_name_formats`` and ``base_branches``
# are looked up by.
LANE_TOKEN = "ww.task.lane"
RUN_TOKEN = "ww.task.run"
FORMAT_TOKENS = (TASK_ID, WORKFLOW_TOKEN, LANE_TOKEN, RUN_TOKEN)
DEFAULT_COMMIT_FORMAT = "{{ww.task.id}}: {{commit_message}}"
DEFAULT_BRANCH_FORMAT = "{{ww.task.id}}"
_SETTING_KEYS = {
    "commit_format",
    "base_branches",
    "separate_branch",
    "branch_name_formats",
    "worktrees",
    "worktree_dir",
    "worktree_name_format",
    "on_signing_failure",
}
# What the commit handler does when git cannot sign a commit: stop for the
# operator, or commit once more without a signature and say so.
SIGNING_FAILURE_POLICIES = ("operator", "unsigned")
# The fields ``git log`` prints per commit when looking for an operation.
_LOG_FIELDS = 3
# The positional ``args`` of merge-branch.
MERGE_ARGUMENTS = ("branch", "message")
# What git prints when it cannot sign: its own marker, and the signing
# programs' usual wording (gpg, ssh-keygen, ssh-agent signers).
_SIGNING_FAILURE_MARKERS = (
    "failed to write commit object",
    "failed to sign",
    "gpg failed",
    "signing failed",
)


@dataclass(frozen=True)
class Settings:
    """The ``ww/git`` section of ``ww.json``, validated."""

    commit_format: str = DEFAULT_COMMIT_FORMAT
    # Per workflow name, with ``default`` for every other workflow.  A
    # repository with its own conventions states them in its own
    # ``ww.json``, which ww applies for tasks working there.
    base_branches: dict[str, str | tuple[str, ...]] = field(default_factory=dict)
    separate_branch: bool = False
    branch_name_formats: dict[str, str] = field(
        default_factory=lambda: {"default": DEFAULT_BRANCH_FORMAT}
    )
    worktrees: bool = False
    worktree_dir: str | None = None
    worktree_name_format: str = DEFAULT_BRANCH_FORMAT
    on_signing_failure: str = "operator"

    def branch_format(
        self, workflow: str | None, strategy: str | None = None
    ) -> str | None:
        """Return an explicit strategy, or use workflow/default selection."""
        if strategy is not None:
            return self.branch_name_formats.get(strategy)
        if workflow and workflow in self.branch_name_formats:
            return self.branch_name_formats[workflow]
        return self.branch_name_formats.get("default", DEFAULT_BRANCH_FORMAT)

    def base_branch_for(self, workflow: str | None) -> str | tuple[str, ...] | None:
        """Return the workflow's entry, then the ``default`` entry."""
        if workflow and workflow in self.base_branches:
            return self.base_branches[workflow]
        return self.base_branches.get("default")

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit_format": self.commit_format,
            "base_branches": {
                workflow: _base_branch_dict(definition)
                for workflow, definition in self.base_branches.items()
            },
            "separate_branch": self.separate_branch,
            "branch_name_formats": dict(self.branch_name_formats),
            "worktrees": self.worktrees,
            "worktree_dir": self.worktree_dir,
            "worktree_name_format": self.worktree_name_format,
            "on_signing_failure": self.on_signing_failure,
        }


def settings_from(config: Any) -> Settings:
    """Validate one ``ww/git`` settings mapping.

    ww hands the section through untouched, so this is where a typo is caught.
    Unknown keys are an error rather than ignored: a misspelled setting that is
    silently dropped looks configured and changes nothing.
    """
    if not config:
        return Settings()
    if not isinstance(config, dict):
        raise ConfigurationError("ww/git settings must be an object")
    unknown = set(config) - _SETTING_KEYS
    if unknown:
        raise ConfigurationError(
            "ww/git has unknown setting(s): "
            + ", ".join(sorted(unknown))
            + "; known settings: "
            + ", ".join(sorted(_SETTING_KEYS))
        )
    formats = config.get("branch_name_formats", {"default": DEFAULT_BRANCH_FORMAT})
    if not isinstance(formats, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in formats.items()
    ):
        raise ConfigurationError(
            "ww/git branch_name_formats must map workflow names to formats"
        )
    base_branches = config.get("base_branches", {})
    if not isinstance(base_branches, dict) or not all(
        isinstance(workflow, str) and workflow.strip() for workflow in base_branches
    ):
        raise ConfigurationError(
            "ww/git base_branches must map workflow names, or default, to base branches"
        )
    settings = Settings(
        commit_format=_string(config, "commit_format", DEFAULT_COMMIT_FORMAT),
        base_branches={
            workflow: _required_base_branch(definition, f"base_branches[{workflow!r}]")
            for workflow, definition in base_branches.items()
        },
        separate_branch=_bool(config, "separate_branch"),
        branch_name_formats=dict(formats),
        worktrees=_bool(config, "worktrees"),
        worktree_dir=_optional_string(config, "worktree_dir"),
        worktree_name_format=_string(
            config, "worktree_name_format", DEFAULT_BRANCH_FORMAT
        ),
        on_signing_failure=_string(config, "on_signing_failure", "operator"),
    )
    if settings.on_signing_failure not in SIGNING_FAILURE_POLICIES:
        raise ConfigurationError(
            "ww/git on_signing_failure must be one of: "
            + ", ".join(SIGNING_FAILURE_POLICIES)
        )
    _validate_commit_format(settings.commit_format)
    if settings.worktrees and not settings.worktree_dir:
        # Every default is wrong somewhere: inside the repository a worktree
        # dirties the tree is-git-clean guards, and outside it writes to a
        # directory nobody named. Ask instead of guessing.
        raise ConfigurationError(
            "ww/git worktree_dir is required when worktrees is true"
        )
    return settings


def _validate_commit_format(value: str) -> None:
    """Ensure the format can produce one complete commit subject."""
    names = dependencies(value)
    unknown = set(names) - {*FORMAT_TOKENS, "commit_message"}
    if unknown:
        raise ConfigurationError(
            "ww/git commit_format has unknown placeholder(s): "
            + ", ".join(sorted(unknown))
        )
    if names.count("commit_message") != 1:
        raise ConfigurationError(
            "ww/git commit_format must contain exactly one "
            "{{commit_message}} placeholder"
        )


def _string(config: dict[str, Any], key: str, default: str) -> str:
    value = config.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"ww/git {key} must be a non-empty string")
    return value


def _optional_string(config: dict[str, Any], key: str) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"ww/git {key} must be a non-empty string")
    return value


def _required_base_branch(value: Any, path: str) -> str | tuple[str, ...]:
    if isinstance(value, str):
        if value.strip():
            return value
        raise ConfigurationError(f"ww/git {path} must be a non-empty string")
    if not isinstance(value, dict) or set(value) != {"argv"}:
        raise ConfigurationError(
            f"ww/git {path} must be a branch string or an object containing argv"
        )
    argv = value["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(argument, str) and argument for argument in argv)
    ):
        raise ConfigurationError(
            f"ww/git {path}.argv must be a non-empty array of non-empty strings"
        )
    return tuple(argv)


def _base_branch_dict(
    definition: str | tuple[str, ...],
) -> str | dict[str, list[str]]:
    if isinstance(definition, tuple):
        return {"argv": list(definition)}
    return definition


def _bool(config: dict[str, Any], key: str) -> bool:
    value = config.get(key, False)
    if not isinstance(value, bool):
        raise ConfigurationError(f"ww/git {key} must be true or false")
    return value


def _git(
    context: ExtensionContext, *arguments: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd or context.workspace or context.root,
        capture_output=True,
        text=True,
        check=False,
    )


def _failed(result: subprocess.CompletedProcess[str], fallback: str) -> str:
    return (result.stderr or result.stdout).strip() or fallback


def _repository(context: ExtensionContext) -> Path:
    """The primary checkout of the repository this task works in.

    A task's working directory is its project directory, or a worktree of
    it; the ww project root is only the fallback.  Branch operations always
    run in the primary checkout, so a worktree resolves back through its
    shared git directory.
    """
    start = (context.workspace or context.root).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=start,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = start / common
        common = common.resolve()
        if common.name == ".git":
            return common.parent
    return start


def _workspace_root(context: ExtensionContext) -> tuple[Path | None, str | None]:
    """Resolve and verify the Git worktree this handler is allowed to touch."""
    workspace = (context.workspace or context.root).resolve()
    top_level = _git(context, "rev-parse", "--show-toplevel")
    if top_level.returncode:
        return None, _failed(top_level, "could not determine Git worktree root")
    root = Path(top_level.stdout.strip()).resolve()
    if root != workspace:
        return (
            None,
            f"selected workspace {workspace} is not the Git worktree root {root}",
        )
    return root, None


def _paths_inside(root: Path, paths: tuple[str, ...]) -> str | None:
    """Reject paths that do not resolve under the selected worktree root."""
    for path in paths:
        candidate = Path(path)
        if not path or candidate.is_absolute():
            return f"Git reported unsafe worktree path {path!r}"
        try:
            (root / candidate).resolve().relative_to(root)
        except ValueError:
            return f"Git reported path outside the selected worktree: {path!r}"
    return None


def _status_paths(output: str) -> tuple[str, ...]:
    """Read every pathname from Git's NUL-delimited porcelain v1 output."""
    fields = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        if not field:
            index += 1
            continue
        if len(field) <= _PORCELAIN_STATUS_WIDTH or field[2] != " ":
            # Git produced malformed porcelain. Treat it as unsafe rather than
            # guessing which part might be a pathname.
            return ()
        status, path = field[:2], field[3:]
        paths.append(path)
        if "R" in status or "C" in status:
            index += 1
            if index >= len(fields) or not fields[index]:
                return ()
            paths.append(fields[index])
        index += 1
    return tuple(paths)


def _workspace_status(
    context: ExtensionContext,
) -> tuple[tuple[str, ...], str | None]:
    """The paths Git reports changed in the task workspace, or why it cannot."""
    root, error = _workspace_root(context)
    if error:
        return (), error
    assert root is not None
    status = _git(context, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status.returncode:
        return (), _failed(status, "git status failed")
    paths = _status_paths(status.stdout)
    if status.stdout and not paths:
        return (), "Git reported malformed status output"
    return paths, _paths_inside(root, paths)


def _validate_commit_workspace(context: ExtensionContext) -> str | None:
    return _workspace_status(context)[1]


def _needs_commit_message(context: ExtensionContext) -> bool:
    """Whether the workspace has anything to commit; Git trouble asks as usual."""
    paths, error = _workspace_status(context)
    return bool(paths) or error is not None


def _validate_staged_paths(context: ExtensionContext) -> str | None:
    root, error = _workspace_root(context)
    if error:
        return error
    assert root is not None
    staged = _git(context, "diff", "--cached", "--name-only", "-z")
    if staged.returncode:
        return _failed(staged, "could not inspect staged paths")
    paths = tuple(path for path in staged.stdout.split("\0") if path)
    return _paths_inside(root, paths)


def _tokens(context: ExtensionContext) -> dict[str, str]:
    return {
        TASK_ID: context.task_id or "",
        WORKFLOW_TOKEN: context.workflow or "",
        LANE_TOKEN: _lane(context) or "",
        RUN_TOKEN: context.run_id or "",
    }


def _render_for_task(context: ExtensionContext, template: str) -> str:
    """Render ``template`` for the task as one name without the ID's slash.

    A child task (``parent/child``) renders the template with the parent ID
    and appends ``-<child>``, so its name sits beside the parent's rather than
    under it; a template that renders empty stays empty for the caller to
    refuse.
    """
    tokens = _tokens(context)
    task_id = context.task_id or ""
    if "/" not in task_id:
        return interpolate(template, tokens).strip()
    parent_id, _, child_id = task_id.rpartition("/")
    parent = interpolate(template, {**tokens, TASK_ID: parent_id}).strip()
    return f"{parent}-{child_id}" if parent else ""


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _current_branch(context: ExtensionContext, cwd: Path | None = None) -> str:
    result = _git(context, "rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)
    return result.stdout.strip()


def _branch_exists(
    context: ExtensionContext, branch: str, cwd: Path | None = None
) -> bool:
    return (
        _git(
            context,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            cwd=cwd,
        )
    ).returncode == 0


def _root_uses_task_branch(context: ExtensionContext, task_branch: str) -> bool:
    """Whether the project root is already the configured task workspace.

    ``task_branch`` is rendered by :func:`_task_branch` from the configured
    workflow-specific branch format. No conventional base or development
    branch names participate in workspace selection.
    """
    return _current_branch(context, cwd=_repository(context)) == task_branch


def _worktree_path(context: ExtensionContext, settings: Settings, name: str) -> Path:
    directory = Path(settings.worktree_dir or "")
    if not directory.is_absolute():
        directory = _repository(context) / directory
    return directory / name


def _worktree_name(context: ExtensionContext, settings: Settings) -> str:
    """The task's worktree directory name, always a single path component.

    A child's name is its parent's rendered name plus the child ID, as its
    branch is; any other name that still holds a separator has each one
    replaced by ``-``. Names a refusal must see (empty, ``.``, absolute) are
    returned unchanged for the caller to reject.
    """
    name = _render_for_task(context, settings.worktree_name_format)
    if name and not Path(name).is_absolute() and "/" in name:
        name = name.replace("/", "-")
    return name


def _reserved_paths(context: ExtensionContext) -> tuple[Path, ...]:
    """The worktree this task would own, so ww never reuses its ID elsewhere."""
    settings = _task_settings(context)
    if not settings.worktrees or not settings.worktree_dir:
        return ()
    name = _worktree_name(context, settings)
    relative = Path(name)
    if (
        not name
        or relative.is_absolute()
        or any(part in {".", ".."} for part in relative.parts)
    ):
        return ()
    return (_worktree_path(context, settings, name),)


def _record_branch(context: ExtensionContext, **fields: Any) -> None:
    context.store.append_line(
        BRANCHES_FILE,
        json.dumps({**fields, "recorded_at": _now()}, sort_keys=True),
    )


def _recorded_branch(context: ExtensionContext, task_id: str) -> dict[str, Any] | None:
    for line in reversed(context.store.read_lines(BRANCHES_FILE)):
        if not line:
            continue
        record = json.loads(line)
        if record.get("task_id") == task_id:
            return record
    return None


def _record_commit(context: ExtensionContext, record: dict[str, Any]) -> None:
    """Record one commit without racing another recovery of the operation."""
    operation_id = record.get("operation_id")
    if not operation_id:
        context.store.append_line(COMMITS_FILE, json.dumps(record, sort_keys=True))
        return

    def update(current: str | None) -> str:
        lines = current.splitlines() if current else []
        if any(
            json.loads(line).get("operation_id") == operation_id
            for line in lines
            if line
        ):
            return current or ""
        lines.append(json.dumps(record, sort_keys=True))
        return "\n".join(lines) + "\n"

    context.store.update_text(COMMITS_FILE, update)


def _parent_branch(context: ExtensionContext) -> str | None:
    """Return the branch recorded for a direct parent task, if any."""
    if not context.task_id or "/" not in context.task_id:
        return None
    parent_task_id, _, _ = context.task_id.rpartition("/")
    record = _recorded_branch(context, parent_task_id)
    branch = record.get("branch") if record else None
    return branch if isinstance(branch, str) and branch else None


def _resolve_base_branch(
    context: ExtensionContext, definition: str | tuple[str, ...] | None
) -> tuple[str | None, str | None]:
    """Resolve a literal or argv-backed base branch definition."""
    if definition is None:
        return None, None
    if isinstance(definition, str):
        return definition, None
    argv = tuple(interpolate(argument, _tokens(context)) for argument in definition)
    try:
        result = subprocess.run(
            argv,
            cwd=_repository(context),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        return None, f"base branch command could not start: {error}"
    if result.returncode:
        return None, _failed(result, "base branch command failed")
    output = result.stdout.strip()
    if not output:
        return None, "base branch command returned an empty branch name"
    if len(output.splitlines()) != 1:
        return None, "base branch command must return exactly one line"
    return output, None


def _branch_name(
    context: ExtensionContext, branch_format: str
) -> tuple[str | None, str | None, str | None]:
    """Render the task's branch from ``branch_format``: (branch, parent, error).

    A child task's branch is its parent's recorded branch plus the child ID,
    whatever the format says; without a recorded parent branch it is the
    format rendered for the parent plus the child ID, so a child's slash
    never nests its branch under the parent's name.
    """
    branch = _render_for_task(context, branch_format)
    if not branch:
        return None, None, "branch name format rendered empty"
    parent_branch = _parent_branch(context)
    if parent_branch and context.task_id:
        child_id = context.task_id.rpartition("/")[2]
        branch = f"{parent_branch}-{child_id}"
    return branch.lower(), parent_branch, None


def _trusted_base(context: ExtensionContext, branch: str) -> str | None:
    """The base recorded for ``branch``, while that branch still exists.

    A record outlives its task: after task state is cleaned up, a generated
    ID can be handed out again, and the old task's record (perhaps a hotfix
    from another base) must not choose the new task's base.  Only a record
    naming this very branch, which still exists, is the task's own.
    """
    record = _recorded_branch(context, context.task_id or "")
    if record is None or record.get("branch") != branch:
        return None
    base = record.get("base")
    if not isinstance(base, str) or not base:
        return None
    if not _branch_exists(context, branch, cwd=_repository(context)):
        return None
    return base


def _lane(context: ExtensionContext) -> str | None:
    """The workflow whose ``branch_name_formats`` and ``base_branches`` apply.

    A workflow that takes a lane's hooks (``hooks_from``) branches as that
    lane; records and ``{{ww.task.workflow}}`` keep the workflow's own name,
    and ``{{ww.task.lane}}`` gives formats and base-branch commands the lane.
    """
    return context.lane or context.workflow


def _task_branch(
    context: ExtensionContext, settings: Settings
) -> tuple[str | None, str | None, str | None]:
    """Resolve the branch and base shared by branch/worktree handlers."""
    if not context.task_id:
        return None, None, "a task is required to name a branch"
    if (
        context.workflow == "ww-scriptize-rules"
        and "default" not in settings.base_branches
    ):
        return (
            None,
            None,
            (
                "ww-scriptize-rules requires extensions.ww/git.base_branches.default "
                "in ww.json"
            ),
        )
    strategy = context.values.get(BRANCH_NAMING_STRATEGY)
    branch_format = settings.branch_format(_lane(context), strategy)
    if branch_format is None:
        return None, None, f"branch naming strategy not found: {strategy}"
    branch, parent_branch, error = _branch_name(context, branch_format)
    if error or branch is None:
        return None, None, error
    if parent_branch:
        return branch, parent_branch, None
    recorded_base = _trusted_base(context, branch)
    if recorded_base:
        return branch, recorded_base, None
    configured_base, base_error = _resolve_base_branch(
        context, settings.base_branch_for(_lane(context))
    )
    if base_error:
        return None, None, base_error
    base = configured_base or _current_branch(context, cwd=_repository(context))
    return branch, base, None


def _task_settings(context: ExtensionContext) -> Settings:
    """Scriptizing always branches from the default, independent of lanes."""
    settings = settings_from(context.config)
    if context.workflow == "ww-scriptize-rules":
        settings = replace(
            settings,
            separate_branch=True,
            base_branches={
                key: value
                for key, value in settings.base_branches.items()
                if key == "default"
            },
        )
    return settings


def _claims_task(context: ExtensionContext) -> bool:
    """Whether ww/git still holds this task ID: a record or a task branch.

    Either outlives the task state, so a generated ID that has one is not
    handed out again.  Every configured branch format is tried, since the
    branch strategy of a future start is not known yet.
    """
    task_id = context.task_id
    if not task_id:
        return False
    if _recorded_branch(context, task_id) is not None:
        return True
    settings = _task_settings(context)
    repository = _repository(context)
    for branch_format in dict.fromkeys(
        (DEFAULT_BRANCH_FORMAT, *settings.branch_name_formats.values())
    ):
        branch, _parent, error = _branch_name(context, branch_format)
        if not error and branch and _branch_exists(context, branch, cwd=repository):
            return True
    return False


def _forget_task(context: ExtensionContext) -> None:
    """Drop the branch and commit records of the task and its children."""
    task_id = context.task_id
    if not task_id:
        return

    def belongs(line: str) -> bool:
        if not line:
            return False
        owner = json.loads(line).get("task_id")
        return isinstance(owner, str) and (
            owner == task_id or owner.startswith(f"{task_id}/")
        )

    def without_task(current: str | None) -> str:
        lines = [
            line for line in (current or "").splitlines() if line and not belongs(line)
        ]
        return "\n".join(lines) + "\n" if lines else ""

    for name in (BRANCHES_FILE, COMMITS_FILE):
        if context.store.read_text(name) is not None:
            context.store.update_text(name, without_task)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


def _without_rendered_prefix(
    message: str, commit_format: str, context: ExtensionContext
) -> str:
    """Drop a prefix the agent already wrote that the format is about to add.

    Agents tend to start a message with the task ID even though the format
    supplies it; rendering both would repeat it in the subject.
    """
    prefix_template, _, _ = commit_format.partition("{{commit_message}}")
    prefix = interpolate(prefix_template, _tokens(context)).strip()
    if prefix and message.startswith(prefix):
        stripped = message[len(prefix) :].strip()
        if stripped:
            return stripped
    return message


def _subject(context: ExtensionContext, settings: Settings, message: str) -> str:
    """A commit's subject: ``message`` rendered through ``commit_format``."""
    message = _without_rendered_prefix(message.strip(), settings.commit_format, context)
    return interpolate(
        settings.commit_format, {**_tokens(context), "commit_message": message}
    ).strip()


def _commit_message_error(values: Mapping[str, str]) -> str | None:
    """The one shape a subject must have; checked when supplied and when run."""
    message = values.get("commit_message", "").strip()
    if not message:
        return "commit_message is required"
    if "\n" in message or "\r" in message:
        return "commit_message must be a single line"
    return None


def _commit(context: ExtensionContext) -> ExtensionResult:
    settings = _task_settings(context)
    # A retry may arrive after git committed but before ww recorded the
    # result.  The operation trailer gives the checker a durable identity.
    if context.operation_id:
        existing = _check_commit(context)
        if existing.status == "succeeded" and existing.result is not None:
            return existing.result
        if existing.status == "unknown":
            return ExtensionResult(
                False,
                error=(
                    existing.error
                    or "Git could not establish the interrupted operation outcome"
                ),
            )
    workspace_error = _validate_commit_workspace(context)
    if workspace_error:
        return ExtensionResult(False, error=workspace_error)
    staged = _git(context, "add", ".")
    if staged.returncode:
        return ExtensionResult(False, error=_failed(staged, "git add failed"))
    staged_error = _validate_staged_paths(context)
    if staged_error:
        return ExtensionResult(False, error=staged_error)
    has_staged_changes = _git(context, "diff", "--cached", "--quiet")
    if has_staged_changes.returncode == 0:
        # A round that changed nothing is a normal outcome, not a failure:
        # succeed without a commit and without running project commit hooks.
        return ExtensionResult(
            True, output="nothing to commit; the workspace has no changes"
        )
    if has_staged_changes.returncode != 1:
        return ExtensionResult(
            False,
            error=_failed(has_staged_changes, "could not inspect staged changes"),
        )
    # Only a commit needs its message: the precondition lets ww run this
    # without asking for one when the workspace is clean.
    error = _commit_message_error(context.values)
    if error is not None:
        return ExtensionResult(False, error=error)
    subject = _subject(context, settings, context.values["commit_message"])
    if not subject:
        return ExtensionResult(False, error="commit_format rendered an empty message")
    commit_args = ["commit", "-m", subject]
    if context.operation_id:
        commit_args.extend(["-m", _operation_marker(context)])
    committed = _git(context, *commit_args)
    signed = True
    if (
        committed.returncode
        and settings.on_signing_failure == "unsigned"
        and _signing_failed(context, committed)
    ):
        # The signing agent refused (a locked key agent, for instance): the
        # operator allowed an unsigned commit rather than a stop.
        committed = _git(context, "-c", "commit.gpgsign=false", *commit_args)
        signed = False
    if committed.returncode:
        return ExtensionResult(False, error=_failed(committed, "git commit failed"))
    revision = _git(context, "rev-parse", "HEAD")
    record = {
        "sha": revision.stdout.strip(),
        "message": subject,
        "branch": _current_branch(context),
        "task_id": context.task_id,
        "run_id": context.run_id,
        "workflow": context.workflow,
        "committed_at": _now(),
    }
    if context.operation_id:
        record["operation_id"] = context.operation_id
    if not signed:
        record["signed"] = False
    _record_commit(context, record)
    output = f"{record['sha'][:12]} {subject}"
    if not signed:
        output += (
            " (unsigned: git could not sign it, and on_signing_failure allows this)"
        )
    return ExtensionResult(True, output=output)


def _signing_failed(
    context: ExtensionContext, result: subprocess.CompletedProcess[str]
) -> bool:
    """Whether a failed commit failed because git could not sign it."""
    enabled = _git(context, "config", "--bool", "commit.gpgsign")
    if enabled.stdout.strip() != "true":
        return False
    message = (result.stderr + result.stdout).lower()
    return any(marker in message for marker in _SIGNING_FAILURE_MARKERS)


def _check_commit(context: ExtensionContext) -> ExtensionCheckResult:
    """Find a commit made by a previous attempt of this operation.

    Returns ``succeeded`` only for one matching operation marker,
    ``not_succeeded`` when no marker exists, and ``unknown`` when Git or the
    commit record cannot establish a safe outcome.
    """
    found = _operation_commit(context)
    if isinstance(found, ExtensionCheckResult):
        return found
    sha, subject = found
    try:
        record = {
            "sha": sha,
            "message": subject,
            "branch": _current_branch(context),
            "task_id": context.task_id,
            "run_id": context.run_id,
            "workflow": context.workflow,
            "committed_at": _now(),
            "operation_id": context.operation_id,
        }
        # Recording is itself idempotent and the duplicate check shares one
        # lock with the write, so concurrent recovery attempts cannot append
        # the same operation twice.
        _record_commit(context, record)
        return ExtensionCheckResult.succeeded(
            ExtensionResult(True, output=f"{sha[:12]} {subject}")
        )
    except Exception as error:  # noqa: BLE001 - checker must remain tri-state
        return ExtensionCheckResult.unknown(f"could not record Git result: {error}")


def _merge_arguments(context: ExtensionContext) -> tuple[str, str] | str:
    """The branch to merge and the merge message, or why they are unusable."""
    if len(context.arguments) != len(MERGE_ARGUMENTS):
        return "merge-branch takes two args: the branch to merge and the message"
    branch, message = (argument.strip() for argument in context.arguments)
    if not branch:
        return "the branch to merge rendered empty"
    if branch.startswith("-") or any(character.isspace() for character in branch):
        return f"not a branch name: {branch!r}"
    if not message or "\n" in message or "\r" in message:
        return "the merge message must be one non-empty line"
    return branch, message


def _merge_in_progress(context: ExtensionContext) -> bool:
    return _git(context, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0


def _git_path(context: ExtensionContext, name: str) -> Path | None:
    """Where git keeps ``name`` in the workspace's git directory."""
    located = _git(context, "rev-parse", "--git-path", name)
    if located.returncode:
        return None
    path = Path(located.stdout.strip())
    if not path.is_absolute():
        path = (context.workspace or context.root) / path
    return path


def _own_merge_in_progress(context: ExtensionContext) -> bool:
    """Whether the merge in progress was started by this very operation.

    Its message, which git keeps in ``MERGE_MSG`` until the merge concludes,
    then carries this operation's trailer.
    """
    if not context.operation_id:
        return False
    path = _git_path(context, "MERGE_MSG")
    if path is None:
        return False
    try:
        message = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return _operation_marker(context) in message.splitlines()


# What git keeps while a rebase, cherry-pick or revert stands in progress.
_SEQUENCER_STATES = {
    "rebase-merge": "a rebase",
    "rebase-apply": "a rebase or `git am`",
    "CHERRY_PICK_HEAD": "a cherry-pick",
    "REVERT_HEAD": "a revert",
}


def _merge_target_error(context: ExtensionContext) -> str | None:
    """Why the workspace cannot receive a merge: no branch, or another operation."""
    for name, operation in _SEQUENCER_STATES.items():
        path = _git_path(context, name)
        if path is not None and path.exists():
            return (
                f"{operation} is in progress in the workspace; conclude or "
                "abort it, then retry"
            )
    if _git(context, "symbolic-ref", "-q", "HEAD").returncode:
        return (
            "HEAD is detached, so a merge would land on no branch; check out "
            "the branch to merge into, then retry"
        )
    return None


def _lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [path for path in result.stdout.split("\0") if path]


def _blob(context: ExtensionContext, revision: str, path: str) -> str | None:
    found = _git(context, "rev-parse", "-q", "--verify", f"{revision}:{path}")
    return found.stdout.strip() if found.returncode == 0 else None


def _index_blob(context: ExtensionContext, path: str) -> str | None:
    listed = _git(context, "ls-files", "-s", "-z", "--", path)
    for entry in _lines(listed):
        details, _, name = entry.partition("\t")
        # ``<mode> <blob> <stage>``: stage 0 is a merged entry.
        _, blob, stage = (details.split() + ["", "", ""])[:3]
        if name == path and stage == "0":
            return blob
    return None


def _clean_merge_blob(
    context: ExtensionContext, base: str, ours: str, theirs: str
) -> str | None:
    """The blob a clean three-way merge of three blobs gives, or ``None``."""
    with tempfile.TemporaryDirectory(prefix="ww-merge-") as directory:
        files = []
        for name, blob in (("ours", ours), ("base", base), ("theirs", theirs)):
            content = subprocess.run(
                ["git", "cat-file", "blob", blob],
                cwd=context.workspace or context.root,
                capture_output=True,
                check=False,
            )
            if content.returncode:
                return None
            file = Path(directory) / name
            file.write_bytes(content.stdout)
            files.append(str(file))
        merged = _git(context, "merge-file", "-q", *files)
        if merged.returncode:
            return None
        hashed = _git(context, "hash-object", "--", files[0])
    return hashed.stdout.strip() if hashed.returncode == 0 else None


def _merge_untouched(context: ExtensionContext) -> bool:
    """Whether the merge in progress still holds only what git's merge left.

    Every unmerged file still carries conflict markers, no other file changed
    in the work tree, and every staged change is one the merge itself made:
    the merged branch's version of a file only it changed, or the clean
    three-way merge of a file both sides changed. Anything else is the
    operator's work, which aborting would discard; a case this cannot tell
    apart, such as a rename, counts as touched.
    """
    base_found = _git(context, "merge-base", "HEAD", "MERGE_HEAD")
    if base_found.returncode:
        return False
    base = base_found.stdout.strip()
    unmerged = set(
        _lines(_git(context, "diff", "--name-only", "-z", "--diff-filter=U"))
    )
    root = context.workspace or context.root
    for path in unmerged:
        try:
            text = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        if not any(line.startswith("<<<<<<< ") for line in text.splitlines()):
            return False
    if set(_lines(_git(context, "diff", "--name-only", "-z"))) - unmerged:
        return False
    staged = _git(context, "diff", "--cached", "--name-only", "-z", "HEAD")
    if staged.returncode:
        return False
    for path in set(_lines(staged)) - unmerged:
        original = _blob(context, base, path)
        ours = _blob(context, "HEAD", path)
        theirs = _blob(context, "MERGE_HEAD", path)
        index = _index_blob(context, path)
        if theirs == original:
            # The merged branch did not change it: only the operator could.
            return False
        if ours == original:
            expected = theirs
        elif original is None or ours is None or theirs is None:
            return False
        else:
            expected = _clean_merge_blob(context, original, ours, theirs)
        if expected is None or index != expected:
            return False
    return True


def _abort_merge(context: ExtensionContext) -> str | None:
    """Abort the merge in progress; the error when git cannot."""
    aborted = _git(context, "merge", "--abort")
    if aborted.returncode:
        return _failed(aborted, "git merge --abort failed")
    return None


def _merge(context: ExtensionContext) -> ExtensionResult:
    """Merge a branch into the workspace's branch with ``git merge --no-ff``.

    A conflict is never resolved here: the merge is aborted and the handler
    fails naming the conflicting files, so the task stops for the operator.
    """
    settings = _task_settings(context)
    parsed = _merge_arguments(context)
    if isinstance(parsed, str):
        return ExtensionResult(False, error=parsed)
    branch, message = parsed
    subject = _subject(context, settings, message)
    if not subject:
        return ExtensionResult(False, error="commit_format rendered an empty message")
    # As for git-commit: the operation trailer lets a retry recognize the
    # merge commit an interrupted attempt already made.
    if context.operation_id:
        existing = _check_merge(context)
        if existing.status == "succeeded" and existing.result is not None:
            return existing.result
        if existing.status == "unknown":
            return ExtensionResult(
                False,
                error=(
                    existing.error
                    or "Git could not establish the interrupted operation outcome"
                ),
            )
    _, workspace_error = _workspace_root(context)
    if workspace_error:
        return ExtensionResult(False, error=workspace_error)
    target_error = _merge_target_error(context)
    if target_error:
        return ExtensionResult(False, error=target_error)
    if _merge_in_progress(context):
        if not _own_merge_in_progress(context):
            return ExtensionResult(
                False,
                error=(
                    "a merge is already in progress in the workspace; conclude "
                    "it or run `git merge --abort`, then retry"
                ),
            )
        if not _merge_untouched(context):
            # The operator has worked on it since: their resolution is kept.
            return ExtensionResult(
                False,
                error=(
                    f"this step's merge of {branch} is in progress and was "
                    "worked on since (conflicts resolved or changes staged); "
                    "conclude it with `git commit`, or discard it with "
                    "`git merge --abort`, then retry"
                ),
            )
        # An interrupted attempt of this operation stopped mid-merge on a
        # workspace it had found clean, and nothing was done to it since:
        # undo it and merge again.
        abort_error = _abort_merge(context)
        if abort_error:
            return ExtensionResult(False, error=abort_error)
    status = _git(context, "status", "--porcelain")
    if status.returncode:
        return ExtensionResult(False, error=_failed(status, "git status failed"))
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if changed:
        return ExtensionResult(
            False,
            error=(
                f"working tree has {len(changed)} uncommitted change(s); "
                f"commit or remove them before merging {branch}"
            ),
        )
    tip = _git(context, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}")
    if tip.returncode:
        return ExtensionResult(False, error=f"branch {branch!r} does not exist")
    tip_sha = tip.stdout.strip()
    target = _current_branch(context)
    if _git(context, "merge-base", "--is-ancestor", tip_sha, "HEAD").returncode == 0:
        return ExtensionResult(
            True,
            output=f"{branch} is already merged into {target}; nothing to merge",
            values={"merge_commit": ""},
        )
    merge_args = ["merge", "--no-ff", "--no-edit", "-m", subject]
    if context.operation_id:
        merge_args.extend(["-m", _operation_marker(context)])
    merge_args.append(branch)
    merged = _git(context, *merge_args)
    signed = True
    if (
        merged.returncode
        and settings.on_signing_failure == "unsigned"
        and _signing_failed(context, merged)
    ):
        # git merged the tree but could not sign the commit, and left the
        # merge in progress: undo it and merge once more without signing.
        if _merge_in_progress(context):
            abort_error = _abort_merge(context)
            if abort_error:
                return ExtensionResult(False, error=abort_error)
        merged = _git(context, "-c", "commit.gpgsign=false", *merge_args)
        signed = False
    if merged.returncode:
        return ExtensionResult(False, error=_merge_failure(context, branch, merged))
    if _merge_in_progress(context):
        return ExtensionResult(
            False,
            error=(
                f"git reported merging {branch} as done, but a merge is still "
                "in progress (MERGE_HEAD exists); conclude or abort it"
            ),
        )
    head = _git(context, "rev-parse", "HEAD").stdout.strip()
    second_parent = _git(context, "rev-parse", "--verify", "--quiet", "HEAD^2")
    if second_parent.stdout.strip() != tip_sha:
        return ExtensionResult(
            False,
            error=f"HEAD {head[:12]} is not a merge commit of {branch}",
        )
    record: dict[str, Any] = {
        "sha": head,
        "message": subject,
        "branch": target,
        "merged": branch,
        "task_id": context.task_id,
        "run_id": context.run_id,
        "workflow": context.workflow,
        "committed_at": _now(),
    }
    if context.operation_id:
        record["operation_id"] = context.operation_id
    if not signed:
        record["signed"] = False
    _record_commit(context, record)
    output = f"{head[:12]} {subject}"
    if not signed:
        output += (
            " (unsigned: git could not sign it, and on_signing_failure allows this)"
        )
    return ExtensionResult(True, output=output, values={"merge_commit": head})


def _merge_failure(
    context: ExtensionContext,
    branch: str,
    result: subprocess.CompletedProcess[str],
) -> str:
    """Why the merge failed, after aborting whatever it left in progress."""
    conflicted = _git(context, "diff", "--name-only", "--diff-filter=U", "-z")
    files = [path for path in conflicted.stdout.split("\0") if path]
    if not _merge_in_progress(context):
        return _failed(result, "git merge failed")
    abort_error = _abort_merge(context)
    state = (
        "the merge was aborted, so the workspace is as it was before"
        if abort_error is None
        else f"aborting the merge failed ({abort_error}); the workspace is mid-merge"
    )
    if files:
        return (
            f"merging {branch} conflicts in: {', '.join(files)}; {state}. "
            "Resolve the conflicts, then retry"
        )
    return f"{_failed(result, 'git merge failed')}; {state}"


def _check_merge(context: ExtensionContext) -> ExtensionCheckResult:
    """Find the merge commit made by a previous attempt of this operation."""
    found = _operation_commit(context)
    if isinstance(found, ExtensionCheckResult):
        return found
    sha, subject = found
    parsed = _merge_arguments(context)
    try:
        record: dict[str, Any] = {
            "sha": sha,
            "message": subject,
            "branch": _current_branch(context),
            "merged": parsed[0] if isinstance(parsed, tuple) else None,
            "task_id": context.task_id,
            "run_id": context.run_id,
            "workflow": context.workflow,
            "committed_at": _now(),
            "operation_id": context.operation_id,
        }
        _record_commit(context, record)
        return ExtensionCheckResult.succeeded(
            ExtensionResult(
                True, output=f"{sha[:12]} {subject}", values={"merge_commit": sha}
            )
        )
    except Exception as error:  # noqa: BLE001 - checker must remain tri-state
        return ExtensionCheckResult.unknown(f"could not record Git result: {error}")


def _operation_commit(
    context: ExtensionContext,
) -> tuple[str, str] | ExtensionCheckResult:
    """The one commit whose ``WW-Operation`` trailer names this operation.

    Returns its sha and subject, or the checker result when there is not
    exactly one: ``not_succeeded`` for none, ``unknown`` when Git or the
    operation identity cannot establish a safe outcome.
    """
    if not context.operation_id:
        return ExtensionCheckResult.unknown("operation ID is missing")
    # Each commit ends with a record separator: a body spans lines, so a
    # line break cannot tell one commit from the next.
    log = _git(context, "log", "--all", "--format=%H%x00%s%x00%B%x1e")
    if log.returncode:
        return ExtensionCheckResult.unknown(_failed(log, "git log failed"))
    marker = _operation_marker(context)
    matches = []
    for entry in log.stdout.split("\x1e"):
        fields = entry.strip("\n").split("\x00")
        if len(fields) != _LOG_FIELDS:
            continue
        sha, subject, body = fields
        if marker in body.splitlines():
            matches.append((sha.strip(), subject.strip()))
    if len(matches) > 1:
        return ExtensionCheckResult.unknown(
            f"multiple Git commits claim operation {context.operation_id!r}"
        )
    if not matches:
        return ExtensionCheckResult.not_succeeded()
    return matches[0]


def _operation_marker(context: ExtensionContext) -> str:
    return f"WW-Operation: {context.operation_id}"


def _is_clean(context: ExtensionContext) -> ExtensionResult:
    status = _git(context, "status", "--porcelain")
    if status.returncode:
        return ExtensionResult(False, error=_failed(status, "git status failed"))
    changed = [line for line in status.stdout.splitlines() if line.strip()]
    if changed:
        return ExtensionResult(
            False, error=f"working tree has {len(changed)} uncommitted change(s)"
        )
    return ExtensionResult(True, output="clean")


def _start_branch(context: ExtensionContext) -> ExtensionResult:
    settings = _task_settings(context)
    repository = _repository(context)
    if not settings.separate_branch and not settings.worktrees:
        return ExtensionResult(True, output="configured to work on the current branch")
    branch, base, error = _task_branch(context, settings)
    if error:
        return ExtensionResult(False, error=error)
    assert branch is not None and base is not None
    if settings.worktrees:
        if not _branch_exists(context, branch, cwd=repository):
            created = _git(context, "branch", branch, base, cwd=repository)
            if created.returncode:
                return ExtensionResult(
                    False, error=_failed(created, "git branch failed")
                )
            _record_branch(
                context,
                task_id=context.task_id,
                workflow=context.workflow,
                branch=branch,
                base=base,
                worktree=None,
            )
            return ExtensionResult(True, output=f"created {branch} from {base}")
        if _root_uses_task_branch(context, branch):
            return ExtensionResult(
                True,
                output=f"primary checkout already uses {branch}",
                working_directory=repository,
            )
        if _recorded_branch(context, context.task_id or "") is None:
            _record_branch(
                context,
                task_id=context.task_id,
                workflow=context.workflow,
                branch=branch,
                base=base,
                worktree=None,
            )
        return ExtensionResult(True, output=f"branch {branch} is ready")

    if _branch_exists(context, branch, cwd=repository):
        # Retried, or resumed after a switch: adopt the branch instead of
        # failing, and never re-point it at base.
        if _current_branch(context, cwd=repository) == branch:
            return ExtensionResult(True, output=f"already on {branch}")
        switched = _git(context, "switch", branch, cwd=repository)
        if switched.returncode:
            return ExtensionResult(False, error=_failed(switched, "git switch failed"))
        return ExtensionResult(True, output=f"switched to existing {branch}")
    created = _git(context, "switch", "-c", branch, base, cwd=repository)
    if created.returncode:
        return ExtensionResult(False, error=_failed(created, "git switch -c failed"))
    _record_branch(
        context,
        task_id=context.task_id,
        workflow=context.workflow,
        branch=branch,
        base=base,
        worktree=None,
    )
    return ExtensionResult(True, output=f"created {branch} from {base}")


def _worktree_holding(
    context: ExtensionContext, repository: Path, branch: str
) -> Path | None:
    """The worktree that already has ``branch`` checked out, if any.

    A branch can be checked out in one worktree at a time, so an existing
    checkout of the task branch is this task's workspace, wherever it lives.
    """
    listed = _git(context, "worktree", "list", "--porcelain", cwd=repository)
    if listed.returncode:
        return None
    location: Path | None = None
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            location = Path(line.removeprefix("worktree ").strip())
        elif line == f"branch refs/heads/{branch}" and location is not None:
            return location.resolve()
    return None


def _create_worktree(context: ExtensionContext) -> ExtensionResult:
    """Create a separate checkout only after ``start-task-branch`` prepared it."""
    settings = _task_settings(context)
    repository = _repository(context)
    if not settings.worktrees:
        return ExtensionResult(True, output="worktrees are not enabled")
    branch, base, error = _task_branch(context, settings)
    if error:
        return ExtensionResult(False, error=error)
    assert branch is not None and base is not None
    if _root_uses_task_branch(context, branch):
        return ExtensionResult(
            True,
            output=f"primary checkout already uses {branch}",
            working_directory=repository,
        )
    if not _branch_exists(context, branch, cwd=repository):
        return ExtensionResult(
            False, error="task branch does not exist; run start-task-branch first"
        )
    name = _worktree_name(context, settings)
    path_parts = Path(name).parts
    if (
        not name
        or Path(name).is_absolute()
        or any(part in {".", ".."} for part in path_parts)
    ):
        return ExtensionResult(False, error=f"worktree name format rendered {name!r}")
    path = _worktree_path(context, settings, name)
    if path.exists():
        return ExtensionResult(
            True, output=f"worktree already at {path}", working_directory=path
        )
    existing = _worktree_holding(context, repository, branch)
    if existing is not None and existing != repository.resolve():
        # Left by an earlier round or moved by hand: adopt it rather than
        # failing on git's one-worktree-per-branch rule.
        _record_branch(
            context,
            task_id=context.task_id,
            workflow=context.workflow,
            branch=branch,
            base=base,
            worktree=str(existing),
        )
        return ExtensionResult(
            True,
            output=f"adopted the existing worktree for {branch} at {existing}",
            working_directory=existing,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    added = _git(context, "worktree", "add", str(path), branch, cwd=repository)
    if added.returncode:
        return ExtensionResult(False, error=_failed(added, "git worktree add failed"))
    _record_branch(
        context,
        task_id=context.task_id,
        workflow=context.workflow,
        branch=branch,
        base=base,
        worktree=str(path),
    )
    # The executor persists this path and uses it for all later task work.
    return ExtensionResult(
        True,
        output=f"worktree for {branch} at {path} — do this task's work there",
        working_directory=path,
    )


def _task_workspace_dir(context: ExtensionContext) -> str | None:
    """Select the canonical checkout for this task when Git knows one."""
    settings = _task_settings(context)
    root = _repository(context)
    if not settings.worktrees or not context.task_id:
        return str(root)
    branch, _, error = _task_branch(context, settings)
    if error or branch is None:
        return None
    if _root_uses_task_branch(context, branch):
        return str(root)
    if context.workspace is not None and context.workspace.is_dir():
        return str(context.workspace.resolve())
    record = _recorded_branch(context, context.task_id)
    location = record.get("worktree") if record else None
    if isinstance(location, str) and Path(location).is_dir():
        return str(Path(location).resolve())
    return None


def _recorded_field(context: ExtensionContext, key: str) -> str | None:
    """A field of the task's latest branch record, never a live git lookup.

    The primary checkout and a task's worktree may sit on different branches,
    so what git reports depends on where it is asked; the record does not.
    """
    if not context.task_id:
        return None
    record = _recorded_branch(context, context.task_id)
    value = record.get(key) if record else None
    return value if isinstance(value, str) and value else None


def _branch_variable(context: ExtensionContext) -> str | None:
    """``{{ww.git.branch}}``: the task's branch, once ww/git recorded it."""
    return _recorded_field(context, "branch")


def _base_branch_variable(context: ExtensionContext) -> str | None:
    """``{{ww.git.base_branch}}``: the branch the task's branch was made from."""
    return _recorded_field(context, "base")


def _branch_strategy_variable(context: ExtensionContext) -> str | None:
    """``{{ww.git.branch_strategy}}``: the branch format key the task uses.

    The one ``start --branch-strategy`` chose, else the lane's own entry
    in ``branch_name_formats``, else ``default``.
    """
    strategy = context.values.get(BRANCH_NAMING_STRATEGY)
    if strategy:
        return strategy
    formats = settings_from(context.config).branch_name_formats
    lane = _lane(context)
    if lane and lane in formats:
        return lane
    return "default"


def _remove_worktree(context: ExtensionContext) -> ExtensionResult:
    settings = _task_settings(context)
    if not settings.worktrees:
        return ExtensionResult(True, output="worktrees are not enabled")
    if not context.task_id:
        return ExtensionResult(False, error="a task is required to find its worktree")
    record = _recorded_branch(context, context.task_id)
    location = record.get("worktree") if record else None
    if not location:
        return ExtensionResult(
            True, output=f"no worktree recorded for {context.task_id}"
        )
    if not Path(location).exists():
        return ExtensionResult(True, output=f"worktree {location} is already gone")
    removed = _git(context, "worktree", "remove", location)
    if removed.returncode:
        # git refuses while the checkout holds changes, and so do we: --force
        # would throw away work nobody asked to discard.
        return ExtensionResult(
            False, error=_failed(removed, "git worktree remove failed")
        )
    return ExtensionResult(True, output=f"removed worktree {location}")


def _return_to_base(context: ExtensionContext) -> ExtensionResult:
    settings = _task_settings(context)
    if settings.worktrees:
        return ExtensionResult(True, output="worktrees leave the main checkout alone")
    if not settings.separate_branch:
        return ExtensionResult(True, output="no task branch was created")
    record = _recorded_branch(context, context.task_id or "") or {}
    base = record.get("base")
    if not isinstance(base, str) or not base:
        base, error = _resolve_base_branch(
            context, settings.base_branch_for(_lane(context))
        )
        if error:
            return ExtensionResult(False, error=error)
    if not base:
        return ExtensionResult(True, output="no base branch is configured or recorded")
    if _current_branch(context) == base:
        return ExtensionResult(True, output=f"already on {base}")
    switched = _git(context, "switch", base)
    if switched.returncode:
        return ExtensionResult(False, error=_failed(switched, "git switch failed"))
    return ExtensionResult(True, output=f"switched back to {base}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _commits(context: ExtensionContext) -> str:
    wanted = context.arguments[0] if context.arguments else None
    records = [
        json.loads(line) for line in context.store.read_lines(COMMITS_FILE) if line
    ]
    if wanted:
        records = [record for record in records if record.get("task_id") == wanted]
    if not records:
        return f"No commits recorded{f' for {wanted}' if wanted else ''}."
    return "\n".join(
        f"{record['committed_at']}  {record['sha'][:12]}  "
        f"{record.get('task_id') or '-'}  {record.get('branch') or '-'}  "
        f"{record['message']}"
        for record in records
    )


def _branches(context: ExtensionContext) -> str:
    wanted = context.arguments[0] if context.arguments else None
    records = [
        json.loads(line) for line in context.store.read_lines(BRANCHES_FILE) if line
    ]
    if wanted:
        records = [record for record in records if record.get("task_id") == wanted]
    if not records:
        return f"No branches recorded{f' for {wanted}' if wanted else ''}."
    return "\n".join(
        f"{record['recorded_at']}  {record.get('task_id') or '-'}  "
        f"{record['branch']}  (from {record.get('base') or '-'})"
        + (f"  worktree: {record['worktree']}" if record.get("worktree") else "")
        for record in records
    )


def _branch_strategies(config: Mapping[str, object]) -> tuple[str, ...]:
    """Every branch format key: ``default`` and workflow-specific names."""
    return tuple(settings_from(dict(config)).branch_name_formats)


def _settings(context: ExtensionContext) -> str:
    """Print the resolved settings.

    ww validates the file's shape but not an extension's schema, so a typo
    surfaces when a handler runs. This is the command to check first.
    """
    return json.dumps(settings_from(context.config).to_dict(), indent=2)


EXTENSION = Extension(
    vendor="ww",
    name="git",
    version="0.2.0",
    description="Commit and branch through ww, and keep a record of both.",
    variables=(ExtensionVariable(TASK_WORKSPACE_DIR, _task_workspace_dir),),
    namespace=ExtensionNamespace(
        "git",
        (
            ExtensionVariable("branch", _branch_variable),
            ExtensionVariable("base_branch", _base_branch_variable),
            ExtensionVariable("branch_strategy", _branch_strategy_variable),
        ),
    ),
    reserved_paths=_reserved_paths,
    claims_task=_claims_task,
    forget_task=_forget_task,
    branch_strategies=_branch_strategies,
    handlers=(
        ExtensionHandler(
            "git-commit",
            _commit,
            "Stage everything, commit using commit_format, and record it.",
            provide=(
                ProvidedVariable(
                    "commit_message",
                    "One sentence naming what changed in this round of work, "
                    "since ww's previous commit on this task branch; never "
                    "repeat an earlier commit's message. Leave out the task ID, "
                    "which ww adds, and anything about the agentic environment.",
                ),
            ),
            check=_check_commit,
            validate=_commit_message_error,
            needs_input=_needs_commit_message,
        ),
        ExtensionHandler(
            "merge-branch",
            _merge,
            "Merge a branch into the task's branch with git merge --no-ff, "
            "aborting on a conflict, and record the merge commit.",
            check=_check_merge,
            outputs=("merge_commit",),
            arguments=MERGE_ARGUMENTS,
        ),
        ExtensionHandler(
            "is-git-clean",
            _is_clean,
            "Fail unless the working tree has no uncommitted changes.",
        ),
        ExtensionHandler(
            "start-task-branch",
            _start_branch,
            "Create or select this task's branch from the base branch.",
        ),
        ExtensionHandler(
            "create-worktree",
            _create_worktree,
            "Create or select this task's worktree after its branch exists.",
        ),
        ExtensionHandler(
            "remove-task-worktree",
            _remove_worktree,
            "Remove this task's worktree, refusing while it holds changes.",
        ),
        ExtensionHandler(
            "return-to-base-branch",
            _return_to_base,
            "Switch the main checkout back to the base branch.",
        ),
    ),
    modes=(
        ModeDefinition(
            "conventional-commits",
            (
                "Write commit messages as Conventional Commits.",
                "Use the form <type>(<scope>): <subject>, for example "
                "'fix(plan): reject an unknown hook phase'.",
            ),
        ),
    ),
    commands=(
        ExtensionCommand(
            "commits",
            _commits,
            "List the commits ww recorded, newest last.",
            usage="commits [TASK-ID]",
        ),
        ExtensionCommand(
            "branches",
            _branches,
            "List the branches and worktrees ww opened.",
            usage="branches [TASK-ID]",
        ),
        ExtensionCommand(
            "settings",
            _settings,
            "Print the resolved ww/git settings.",
            usage="settings",
        ),
    ),
)
