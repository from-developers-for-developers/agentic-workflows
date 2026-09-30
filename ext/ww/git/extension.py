# SPDX-License-Identifier: GPL-3.0-or-later
"""Git support for ww, as an extension.

This is the first ww extension, and it deliberately lives in ``<root>/ext/``
rather than inside the ww package: it reaches ww only through
``ww.extensions.api``, so it exercises exactly the path a third-party extension
takes.

What it adds over the equivalent shell handlers is memory and configuration. It
records every commit it makes and every branch it opens, and it reads its
settings from the ``ww/git`` section of ``ww-agentic-workflows.json``:

```json
"extensions": {
  "ww/git": {
    "commit_message": "{{task_id}}: {{commit_message}}",
    "base_branches": {
      "default": "main",
      "bugfix": "develop",
      "task": {"argv": ["./scripts/base-branch", "{{workflow}}"]}
    },
    "use_separate_branch": true,
    "branch_name_formats": {
      "default": "feature/{{task_id}}",
      "bugfix": "hotfix/{{task_id}}"
    },
    "worktrees": false
  }
}
```

Handlers adopt state that already satisfies their request where possible and
refuse destructive recovery: nothing here passes ``--force``, discards a
change, or moves work you did not ask it to move. This is not a general
exactly-once guarantee. ``git-commit`` uses ww's stable operation ID and its
checker to recognize a commit made by an interrupted attempt before retrying.
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
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    ExtensionResult,
    ExtensionVariable,
    ModeDefinition,
    ProvidedVariable,
)
from ww.interpolation import dependencies, interpolate
from ww.variables import BRANCH_NAMING_STRATEGY

COMMITS_FILE = "commits.jsonl"
BRANCHES_FILE = "branches.jsonl"
# ``git status --porcelain`` prefixes each path with two status letters and a space.
_PORCELAIN_STATUS_WIDTH = 3

DEFAULT_COMMIT_FORMAT = "{{task_id}}: {{commit_message}}"
DEFAULT_BRANCH_FORMAT = "{{task_id}}"
_SETTING_KEYS = {
    "commit_message",
    "commit_format",
    "base_branches",
    "use_separate_branch",
    "branch_name_formats",
    "worktrees",
    "worktree_dir",
    "worktree_name_format",
    "on_signing_failure",
}
# What the commit handler does when git cannot sign a commit: stop for the
# operator, or commit once more without a signature and say so.
SIGNING_FAILURE_POLICIES = ("operator", "unsigned")
# What git prints when it cannot sign: its own marker, and the signing
# programs' usual wording (gpg, ssh-keygen, 1Password's op-ssh-sign).
_SIGNING_FAILURE_MARKERS = (
    "failed to write commit object",
    "failed to sign",
    "gpg failed",
    "signing failed",
)


@dataclass(frozen=True)
class Settings:
    """The ``ww/git`` section of ``ww-agentic-workflows.json``, validated."""

    commit_format: str = DEFAULT_COMMIT_FORMAT
    # Per workflow name, with ``default`` for every other workflow.  A
    # repository with its own conventions states them in its own
    # ``ww-agentic-workflows.json``, which ww applies for tasks working there.
    base_branches: dict[str, str | tuple[str, ...]] = field(default_factory=dict)
    use_separate_branch: bool = False
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
            "use_separate_branch": self.use_separate_branch,
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
    if "base_branch" in config:
        raise ConfigurationError(
            'ww/git base_branch is now the "default" entry of base_branches: '
            'use "base_branches": {"default": ...}'
        )
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
    if "commit_message" in config and "commit_format" in config:
        raise ConfigurationError(
            "ww/git settings cannot define both commit_message and commit_format"
        )
    base_branches = config.get("base_branches", {})
    if not isinstance(base_branches, dict) or not all(
        isinstance(workflow, str) and workflow.strip() for workflow in base_branches
    ):
        raise ConfigurationError(
            "ww/git base_branches must map workflow names, or default, to base "
            "branches"
        )
    settings = Settings(
        commit_format=_string(
            config,
            "commit_message" if "commit_message" in config else "commit_format",
            DEFAULT_COMMIT_FORMAT,
        ),
        base_branches={
            workflow: _required_base_branch(definition, f"base_branches[{workflow!r}]")
            for workflow, definition in base_branches.items()
        },
        use_separate_branch=_bool(config, "use_separate_branch"),
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
    unknown = set(names) - {"task_id", "workflow", "run_id", "commit_message"}
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


def _validate_commit_workspace(context: ExtensionContext) -> str | None:
    root, error = _workspace_root(context)
    if error:
        return error
    assert root is not None
    status = _git(context, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status.returncode:
        return _failed(status, "git status failed")
    paths = _status_paths(status.stdout)
    if status.stdout and not paths:
        return "Git reported malformed status output"
    return _paths_inside(root, paths)


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
        "task_id": context.task_id or "",
        "workflow": context.workflow or "",
        "run_id": context.run_id or "",
    }


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


def _reserved_paths(context: ExtensionContext) -> tuple[Path, ...]:
    """The worktree this task would own, so ww never reuses its ID elsewhere."""
    settings = settings_from(context.config)
    if not settings.worktrees or not settings.worktree_dir:
        return ()
    name = interpolate(settings.worktree_name_format, _tokens(context)).strip()
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


def _task_branch(
    context: ExtensionContext, settings: Settings
) -> tuple[str | None, str | None, str | None]:
    """Resolve the branch and base shared by branch/worktree handlers."""
    if not context.task_id:
        return None, None, "a task is required to name a branch"
    strategy = context.values.get(BRANCH_NAMING_STRATEGY)
    branch_format = settings.branch_format(context.workflow, strategy)
    if branch_format is None:
        return None, None, f"branch naming strategy not found: {strategy}"
    branch = interpolate(branch_format, _tokens(context)).strip()
    if not branch:
        return None, None, "branch name format rendered empty"
    parent_branch = _parent_branch(context)
    if parent_branch:
        child_id = context.task_id.rpartition("/")[2]
        branch = f"{parent_branch}-{child_id}"
    branch = branch.lower()
    record = _recorded_branch(context, context.task_id)
    recorded_base = record.get("base") if record else None
    if not isinstance(recorded_base, str) or not recorded_base:
        recorded_base = None
    configured_base = None
    if not parent_branch and not recorded_base:
        configured_base, base_error = _resolve_base_branch(
            context, settings.base_branch_for(context.workflow)
        )
        if base_error:
            return None, None, base_error
    base = (
        parent_branch
        or recorded_base
        or configured_base
        or _current_branch(context, cwd=_repository(context))
    )
    return branch, base, None


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


def _commit_message_error(values: Mapping[str, str]) -> str | None:
    """The one shape a subject must have; checked when supplied and when run."""
    message = values.get("commit_message", "").strip()
    if not message:
        return "commit_message is required"
    if "\n" in message or "\r" in message:
        return "commit_message must be a single line"
    return None


def _commit(context: ExtensionContext) -> ExtensionResult:
    settings = settings_from(context.config)
    error = _commit_message_error(context.values)
    if error is not None:
        return ExtensionResult(False, error=error)
    message = context.values["commit_message"].strip()
    message = _without_rendered_prefix(message, settings.commit_format, context)
    subject = interpolate(
        settings.commit_format, {**_tokens(context), "commit_message": message}
    ).strip()
    if not subject:
        return ExtensionResult(False, error="commit_format rendered an empty message")
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
    commit_args = ["commit", "-m", subject]
    if context.operation_id:
        commit_args.extend(["-m", f"WW-Operation: {context.operation_id}"])
    committed = _git(context, *commit_args)
    signed = True
    if (
        committed.returncode
        and settings.on_signing_failure == "unsigned"
        and _signing_failed(context, committed)
    ):
        # The signing agent refused, as a locked 1Password does overnight:
        # the operator allowed an unsigned commit rather than a stop.
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
    if not context.operation_id:
        return ExtensionCheckResult.unknown("operation ID is missing")
    if not context.operation_id_known:
        return ExtensionCheckResult.unknown(
            "operation ID was synthesized while migrating legacy state"
        )
    log = _git(context, "log", "--all", "--format=%H%x00%s%x00%B")
    if log.returncode:
        return ExtensionCheckResult.unknown(_failed(log, "git log failed"))
    marker = f"WW-Operation: {context.operation_id}"
    fields = log.stdout.split("\x00")
    matches = []
    for index in range(0, len(fields) - 2, 3):
        sha, subject, body = fields[index : index + 3]
        if marker not in body.splitlines():
            continue
        matches.append((sha.strip(), subject.strip()))
    if len(matches) != 1:
        if len(matches) > 1:
            return ExtensionCheckResult.unknown(
                f"multiple Git commits claim operation {context.operation_id!r}"
            )
        return ExtensionCheckResult.not_succeeded()
    sha, subject = matches[0]
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
    settings = settings_from(context.config)
    repository = _repository(context)
    if not settings.use_separate_branch and not settings.worktrees:
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
    settings = settings_from(context.config)
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
    name = interpolate(settings.worktree_name_format, _tokens(context)).strip()
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
    settings = settings_from(context.config)
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


def _remove_worktree(context: ExtensionContext) -> ExtensionResult:
    settings = settings_from(context.config)
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
    settings = settings_from(context.config)
    if settings.worktrees:
        return ExtensionResult(True, output="worktrees leave the main checkout alone")
    if not settings.use_separate_branch:
        return ExtensionResult(True, output="no task branch was created")
    record = _recorded_branch(context, context.task_id or "") or {}
    base = record.get("base")
    if not isinstance(base, str) or not base:
        base, error = _resolve_base_branch(
            context, settings.base_branch_for(context.workflow)
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
    variables=(ExtensionVariable("__task_workspace_dir", _task_workspace_dir),),
    reserved_paths=_reserved_paths,
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
