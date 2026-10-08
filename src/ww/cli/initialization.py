# SPDX-License-Identifier: GPL-3.0-or-later
"""Interactive and flag-driven project initialization."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from ww import claude_permissions
from ww.config.composition import compose_configuration
from ww.config_files import runtime_ignored, settings_levels
from ww.defaults import SKILLS, WW_SKILL_NAME, default_settings, skill_location
from ww.discovery import AGENT_DIRECTORIES
from ww.errors import ConfigurationError, StateError
from ww.executable import (
    DEFAULT_EXECUTABLE,
    PROJECT_LAUNCHER_COMMAND,
    project_command,
)
from ww.hooks import (
    HOOK_AGENTS,
    HookInstallError,
    hooks_installed,
    install_hooks,
)
from ww.output_adapters.terminal import initialization_progress
from ww.project_config import ON_REQUEST, Enabled, compose_settings
from ww.results import InitializationResult
from ww.storage import Storage

from .prompts import (
    _ask_checklist,
    _ask_choice,
    _ask_names,
    _ask_yes_no,
    _interactive_terminal,
)

# The bundled Git extension, whose settings init writes.
GIT_EXTENSION = "ww/git"


def _init_choices(storage: Storage) -> dict[str, object]:
    path = storage.runtime_path / "init-choices.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"Cannot read saved init choices: {error}") from error
    if not isinstance(value, dict):
        raise StateError("Saved init choices must be a JSON object")
    return value


def _save_init_choice(storage: Storage, key: str, value: object) -> None:
    choices = _init_choices(storage)
    choices[key] = value
    storage.locks.atomic_write(
        storage.runtime_path / "init-choices.json",
        json.dumps(choices, indent=2) + "\n",
    )


def install_agent_hooks(
    storage: Storage, args: argparse.Namespace, result: InitializationResult
) -> InitializationResult:
    """Offer ww's hooks to each hook-capable agent this project is set up for.

    An agent counts as set up when its directory exists, which is also where
    init just installed its skills. The answer is remembered per agent, and
    ``--hooks``/``--no-hooks`` decide for every agent without asking, and
    ``--force`` asks again for every agent whose hooks are not installed,
    when there is a terminal to ask at; without one the answers stand. A
    hook installation that fails never fails init: the summary says how to
    add the hooks by hand instead.
    """
    interactive = not args.no_input and not args.json_output and sys.stdin.isatty()
    saved = _init_choices(storage).get("hooks", {})
    choices = dict(saved) if isinstance(saved, dict) else {}
    remembered = {} if args.force and interactive else choices
    created, preserved, actions = (
        list(result.created),
        list(result.preserved),
        list(result.actions),
    )
    explained = False
    for name, agent in HOOK_AGENTS.items():
        if not (storage.root / AGENT_DIRECTORIES[name]).is_dir():
            continue
        if hooks_installed(storage, agent) and args.hooks is not False:
            choices[name] = True
            preserved.append(f"{agent.settings_file} (ww hooks)")
            continue
        wanted = args.hooks if args.hooks is not None else remembered.get(name)
        if wanted is None and interactive:
            if not explained:
                print(
                    "\nww's session-start hook tells an agent session that ww "
                    "coordinates work here and how to\nlist its workflows. It "
                    "only adds one line of context; nothing is blocked.\n"
                )
                explained = True
            try:
                wanted = _ask_yes_no(
                    _init_prompt(60, f"Install ww hooks for {name}? [Y/n]: "), True
                )
            except EOFError:
                # Input ended before the question: leave it open for next time.
                break
        if not isinstance(wanted, bool):
            continue
        choices[name] = wanted
        if not wanted:
            continue
        try:
            installation = install_hooks(storage, agent)
        except (HookInstallError, OSError) as error:
            actions.append(f"Add ww's hooks for {name} by hand: {error}")
            continue
        if installation.changed:
            created.append(f"{agent.settings_file} (added ww hooks)")
    if choices:
        _save_init_choice(storage, "hooks", choices)
    return replace(
        result,
        created=tuple(created),
        preserved=tuple(preserved),
        actions=tuple(actions),
    )


def install_claude_permissions(
    storage: Storage, args: argparse.Namespace, result: InitializationResult
) -> InitializationResult:
    """Offer Bash allow rules for ww's role commands in Claude Code's local file.

    Opt-in: the question's default is no, a terminal is needed to be asked,
    and ``--permissions``/``--no-permissions`` answer without one.  The answer
    is remembered, so a later init refreshes the rules it was given for.  The
    rules name the project wrapper by absolute path; the file is merged, never
    overwritten, and kept out of Git.  A settings file that cannot be merged
    never fails init: the summary says how to add the rules by hand.
    """
    if not (storage.root / AGENT_DIRECTORIES["claudecode"]).is_dir():
        return result
    interactive = not args.no_input and not args.json_output and sys.stdin.isatty()
    choices = _init_choices(storage)
    remembered = None if args.force and interactive else choices.get("permissions")
    wanted = args.permissions if args.permissions is not None else remembered
    if wanted is None and interactive:
        try:
            wanted = _ask_yes_no(
                _init_prompt(
                    65,
                    "Allow ww's role commands for Claude Code in "
                    f"{claude_permissions.SETTINGS_FILE}? [y/N]: ",
                ),
                False,
            )
        except EOFError:
            return result
    if not isinstance(wanted, bool):
        return result
    _save_init_choice(storage, "permissions", wanted)
    if not wanted:
        return result
    created, preserved, actions = (
        list(result.created),
        list(result.preserved),
        list(result.actions),
    )
    rules = claude_permissions.role_rules(storage.root.resolve() / "ww")
    try:
        if claude_permissions.install_rules(storage.root, rules):
            created.append(f"{claude_permissions.SETTINGS_FILE} (added ww permissions)")
        else:
            preserved.append(f"{claude_permissions.SETTINGS_FILE} (ww permissions)")
        if claude_permissions.ensure_ignored(storage.root):
            created.append(f".gitignore (added {claude_permissions.SETTINGS_FILE})")
    except (StateError, OSError) as error:
        actions.append(
            f"Add these to permissions.allow in {claude_permissions.SETTINGS_FILE} "
            f"by hand ({error}): " + ", ".join(rules)
        )
    return replace(
        result,
        created=tuple(created),
        preserved=tuple(preserved),
        actions=tuple(actions),
    )


def _instructions_linked(storage: Storage) -> bool:
    names = {"AGENTS.md"}
    for directory, name in ((".claude", "CLAUDE.md"), (".gemini", "GEMINI.md")):
        if (storage.root / directory).is_dir() or (storage.root / name).exists():
            names.add(name)
    return all(
        (storage.root / name).is_file()
        and "@WW_AGENT_INSTRUCTIONS.md"
        in (storage.root / name).read_text(encoding="utf-8")
        for name in names
    )


def _init_prompt(percent: int, question: str) -> str:
    """Keep progress attached to the question currently being answered."""
    return f"{initialization_progress(percent)} {question}"


def _link_agent_instructions(
    storage: Storage, result: InitializationResult
) -> InitializationResult:
    """Append the approved reference while preserving existing instructions."""
    names = {"AGENTS.md"}
    for directory, filename in ((".claude", "CLAUDE.md"), (".gemini", "GEMINI.md")):
        if (storage.root / directory).is_dir():
            names.add(filename)
    names.update(
        name for name in ("CLAUDE.md", "GEMINI.md") if (storage.root / name).is_file()
    )
    created = list(result.created)
    for name in sorted(names):
        path = storage.root / name
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        if "@WW_AGENT_INSTRUCTIONS.md" in content:
            continue
        separator = (
            "\n\n"
            if content and not content.endswith("\n")
            else "\n"
            if content
            else ""
        )
        storage.locks.atomic_write(
            path, content + separator + "@WW_AGENT_INSTRUCTIONS.md\n"
        )
        created.append(name + (" (added instruction reference)" if content else ""))
    actions = tuple(
        action for action in result.actions if "@WW_AGENT_INSTRUCTIONS.md" not in action
    )
    return replace(result, created=tuple(created), actions=actions)


def _initialization_options(
    storage: Storage, args: argparse.Namespace
) -> tuple[str, str, bool, tuple[tuple[str, str], ...]]:
    interactive = not args.no_input and not args.json_output and sys.stdin.isatty()
    # ``--force`` reopens the remembered questions only where it can ask them
    # again; without a terminal the remembered answers stand, and init adds
    # only what they leave missing.
    force = args.force and interactive
    enabled = _enabled_choice(storage, interactive, force=force)
    task_kind = args.task_id_format
    if task_kind is None and interactive and not _configured_task_format(storage):
        print(
            "Choose the default task ID format:\n"
            "  uuid      TASK-<unique UUID> — unique across projects (default)\n"
            "  digit     TASK-1, TASK-2, ... — sequential project numbers\n"
            "  timestamp TASK-<timestamp> — based on the creation time\n"
        )
        task_kind = _ask_choice(
            _init_prompt(5, "Task ID format [uuid/digit/timestamp] (uuid): "),
            ("uuid", "digit", "timestamp"),
            "uuid",
        )
    workflows = "modes: []\nhandlers: []\nhooks: {}\nworkflows: []\n"

    # Every setting with its default, the answers written over them.
    project = default_settings()
    # The user or local level's own ``enabled`` stands, and a format another
    # level already provides is not repeated in the repo file.
    if enabled is None:
        del project["enabled"]
    else:
        project["enabled"] = enabled
    if task_kind is not None or not _configured_task_format(storage):
        project["task_format"] = "TASK-{{" + (task_kind or "uuid") + "}}"
    else:
        del project["task_format"]
    # A setting init does not ask about, which the user or local level
    # already sets, keeps that value rather than a default over it.
    for name, raw in _settings_by_level(storage).items():
        if name != "repo":
            for key in _UNASKED_SETTINGS & raw.keys():
                project.pop(key, None)
    has_git = (storage.root / ".git").exists()
    if has_git:
        existing_git = _existing_git_settings(storage)
        existing_worktrees = existing_git.get("worktrees")
        worktrees = (
            existing_worktrees
            if isinstance(existing_worktrees, bool)
            else args.worktrees
        )
        if worktrees is None and interactive:
            worktrees = _ask_yes_no(
                _init_prompt(15, "Use Git worktrees? [y/N]: "), False
            )
        worktrees = bool(worktrees)
        existing_directory = existing_git.get("worktree_dir")
        directory = (
            existing_directory
            if isinstance(existing_directory, str) and existing_directory
            else args.worktree_dir
        )
        if worktrees and directory is None and interactive:
            directory = _ask_directory(
                _init_prompt(25, "Worktree directory (./git-worktrees): ")
            )
        directory = directory or "./git-worktrees"
        formats = {"default": "feature/{{ww.task.id}}"}
        existing_formats = existing_git.get("branch_name_formats")
        if isinstance(existing_formats, dict):
            formats.update(existing_formats)
        formats.update(_branch_format_arguments(args.branch_format))
        if interactive:
            for workflow in _workflow_names(storage):
                if workflow in formats:
                    continue
                value = input(
                    _init_prompt(
                        35, f"Branch format for workflow {workflow!r} (blank to skip): "
                    )
                ).strip()
                if value:
                    formats[workflow] = value
        git: dict[str, object] = {
            "commit_format": "{{ww.task.id}}: {{commit_message}}",
            "base_branches": {"default": _git_base_branch(storage.root)},
            "separate_branch": True,
            "branch_name_formats": formats,
            "worktrees": worktrees,
        }
        if worktrees:
            git.update(
                {
                    "worktree_dir": directory,
                    "worktree_name_format": "{{ww.task.id}}",
                }
            )
        project["extensions"] = {GIT_EXTENSION: git}

    ignore_runtime = args.update_gitignore
    # ``--force`` asks every question again, as if nothing were remembered.
    choices = {} if force else _init_choices(storage)
    ignore_path = storage.root / ".gitignore"
    ignored = ignore_path.is_file() and runtime_ignored(
        ignore_path.read_text(encoding="utf-8")
    )
    if ignore_runtime is None:
        ignore_runtime = True if ignored else choices.get("update_gitignore")
    if has_git and ignore_runtime is None and interactive:
        ignore_runtime = _ask_yes_no(
            _init_prompt(45, "Create or update .gitignore to exclude .ww/? [Y/n]: "),
            True,
        )
    if ignore_runtime is None:
        ignore_runtime = True
    if isinstance(ignore_runtime, bool):
        _save_init_choice(storage, "update_gitignore", ignore_runtime)
    if args.link_instructions is None:
        linked = choices.get("link_instructions")
        if _instructions_linked(storage):
            args.link_instructions = True
        elif isinstance(linked, bool):
            args.link_instructions = linked
    if args.link_instructions is None and interactive:
        print(
            "\nWW_AGENT_INSTRUCTIONS.md tells your agents how to work through ww:\n"
            "to start from `./ww discover`, follow each response, and keep going\n"
            "until the workflow is done. Referencing it from the instruction files\n"
            "your agents already read (AGENTS.md, and CLAUDE.md or GEMINI.md where\n"
            "those agents are set up) means they pick it up on their own, so you\n"
            "can just ask for the work instead of explaining ww every session.\n"
            "\n"
            "Answer no to keep those files untouched; you can add the one-line\n"
            "reference yourself later, or name ww in the request when you want it.\n"
        )
        args.link_instructions = _ask_yes_no(
            _init_prompt(
                50, "Add @WW_AGENT_INSTRUCTIONS.md to agent instruction files? [Y/n]: "
            ),
            True,
        )
    if isinstance(args.link_instructions, bool):
        _save_init_choice(storage, "link_instructions", args.link_instructions)
    return (
        workflows,
        json.dumps(project, indent=2) + "\n",
        bool(ignore_runtime),
        _skill_installs(storage, args.skills, interactive, progress=True, force=force),
    )


# The settings init writes with their defaults without asking about them.
_UNASKED_SETTINGS = frozenset(
    {"runtime", "update_check", "limits", "rules", "builtins", "workflows", "projects"}
)


# The ``enabled`` values as the operator types them.
_ENABLED_ANSWERS: dict[str, Enabled] = {
    "true": True,
    ON_REQUEST: ON_REQUEST,
    "false": False,
}


def _enabled_value(value: object) -> Enabled | None:
    if isinstance(value, bool):
        return value
    return ON_REQUEST if value == ON_REQUEST else None


def _enabled_choice(
    storage: Storage, interactive: bool, *, force: bool = False
) -> Enabled | None:
    """Whether agents use ww here by default, only on request, or never.

    The repo file's own ``enabled`` stands. When only the user or local level
    sets it, the repo file gets none, so a choice one person made is not
    committed for the team, and nothing is asked: ``None``. Otherwise it is
    asked once and the answer remembered; ``force`` ignores that remembered
    answer. Without a terminal init writes ``true``.
    """
    levels = _settings_by_level(storage)
    own = _enabled_value(levels.get("repo", {}).get("enabled"))
    if own is not None:
        return own
    if any(
        _enabled_value(raw.get("enabled")) is not None
        for name, raw in levels.items()
        if name != "repo"
    ):
        return None
    remembered = None if force else _init_choices(storage).get("enabled")
    value = _enabled_value(remembered)
    if value is not None:
        return value
    if not interactive:
        return True
    print(
        "Choose when agents use ww in this project:\n"
        "  true        by default, for requests that carry out project work "
        "(default)\n"
        "  on_request  only when the user explicitly asks for ww\n"
        "  false       never\n"
    )
    enabled = _ENABLED_ANSWERS[
        _ask_choice(
            _init_prompt(2, "Use ww [true/on_request/false] (true): "),
            tuple(_ENABLED_ANSWERS),
            "true",
        )
    ]
    _save_init_choice(storage, "enabled", enabled)
    return enabled


def _skill_location(directory: str) -> str:
    """The ``ww`` skill, whose presence marks a directory as already set up."""
    return skill_location(directory, WW_SKILL_NAME)


# "ww skills (ww, noww, ...)": every bundled skill, named in one phrase.
_SKILL_NAMES = f"ww skills ({', '.join(SKILLS)})"


def _agent_directories(storage: Storage) -> tuple[str, ...]:
    """Agent directories from discovery that exist in this project."""
    return tuple(
        directory
        for directory in _known_agent_directories()
        if (storage.root / directory).is_dir()
    )


def _known_agent_directories() -> tuple[str, ...]:
    """Shared agent skills plus each integration-specific settings directory."""
    return (".agents", *dict.fromkeys(AGENT_DIRECTORIES.values()))


def _skill_installs(
    storage: Storage,
    requested: bool | None,
    interactive: bool,
    *,
    progress: bool = False,
    force: bool = False,
) -> tuple[tuple[str, str], ...]:
    """Pair each chosen agent directory with each skill it should hold.

    The directories are chosen once and remembered. The skills are the
    bundled ones the operator accepted, also remembered, so a skill that a
    later ww version bundles is offered once on its own, into the directories
    already chosen, without choosing agents again. ``force`` asks both again,
    offering only what is not installed yet: nothing is ever removed.
    """
    saved = _init_choices(storage).get("agents", {})
    fresh = not any(
        isinstance(saved, dict) and isinstance(saved.get(directory), bool)
        for directory in _known_agent_directories()
    )
    directories = _skill_directories(
        storage, requested, interactive, progress=progress, force=force
    )
    names = _accepted_skills(
        storage,
        directories,
        requested,
        interactive,
        fresh=fresh,
        progress=progress,
        force=force,
    )
    return tuple((directory, name) for directory in directories for name in names)


def _accepted_skills(
    storage: Storage,
    directories: tuple[str, ...],
    requested: bool | None,
    interactive: bool,
    *,
    fresh: bool,
    progress: bool,
    force: bool = False,
) -> tuple[str, ...]:
    """The bundled skills to install, asking only about ones never offered.

    A first run offers every bundled skill through the directory question. A
    project set up before skills were remembered counts a skill found in a
    chosen directory as accepted. Either answer to a new skill is remembered,
    so it is asked about once. ``force`` forgets the answers: a skill found
    in a chosen directory stays accepted, and the others are offered again,
    unless none is installed anywhere, when the directory question offered
    them all.
    """
    if requested is False or not directories:
        return ()
    saved = None if force else _init_choices(storage).get("skills")
    decided: dict[str, bool] = (
        {name: value for name, value in saved.items() if isinstance(value, bool)}
        if isinstance(saved, dict)
        else {}
    )
    if not isinstance(saved, dict):
        present = {
            name
            for name in SKILLS
            for directory in directories
            if (storage.root / skill_location(directory, name)).exists()
        }
        fresh = fresh or (force and not present)
        decided = {
            name: True for name in SKILLS if fresh or requested or name in present
        }
    new = [name for name in SKILLS if name not in decided]
    if new:
        accept = True
        if interactive and not requested:
            label = ", ".join(f"`{name}`" for name in new)
            plural = "s" if len(new) > 1 else ""
            offer = (
                f"Also install the {label} skill{plural}"
                if force
                else f"ww now ships the {label} skill{plural}. Install"
            )
            accept = _ask_yes_no(
                _progress(
                    progress,
                    55,
                    f"{offer} into {', '.join(directories)}? [Y/n]: ",
                ),
                True,
            )
        decided.update({name: accept for name in new})
    _save_init_choice(storage, "skills", decided)
    return tuple(name for name in SKILLS if decided.get(name))


def _skill_directories(
    storage: Storage,
    requested: bool | None,
    interactive: bool,
    *,
    progress: bool = False,
    force: bool = False,
) -> tuple[str, ...]:
    """Choose the agent directories that receive the bundled skills.

    Directories already holding the ``ww`` skill are included so
    initialization reports their files as preserved and adds any skill that
    is missing; storage never overwrites them. ``force`` asks again about
    every other directory, whatever was answered before.
    """
    saved = _init_choices(storage).get("agents", {})
    choices = dict(saved) if isinstance(saved, dict) else {}
    chosen_directories: list[str] = []
    directories = _known_agent_directories()
    undecided: list[tuple[str, bool]] = []
    for directory in directories:
        location = _skill_location(directory)
        exists = (storage.root / directory).is_dir()
        installed = (storage.root / location).exists()
        selected = None if force else choices.get(directory)
        if requested is False:
            selected = False
        elif installed or (requested and exists):
            selected = True
        elif not isinstance(selected, bool) and interactive:
            # Every open question is settled in one place below, so the
            # operator answers a single screen instead of one prompt per agent.
            undecided.append((directory, exists))
            selected = None
        if isinstance(selected, bool):
            choices[directory] = selected
            _save_init_choice(storage, "agents", choices)
        if selected is True:
            chosen_directories.append(directory)
    if undecided:
        chosen = _choose_agent_directories(undecided, progress)
        for directory, _ in undecided:
            choices[directory] = directory in chosen
            if directory in chosen:
                chosen_directories.append(directory)
        _save_init_choice(storage, "agents", choices)
    return tuple(chosen_directories)


def _choose_agent_directories(
    undecided: list[tuple[str, bool]], progress: bool
) -> set[str]:
    """Settle every open agent-directory question, in one screen where we can.

    A terminal we can redraw gets a checklist. A pipe, a dumb terminal, or a
    captured stdin gets plain questions instead: one for each directory that
    already exists, and one shared question for the agents without one.
    """
    if _interactive_terminal():
        return set(
            _ask_checklist(
                f"\nInstall the {_SKILL_NAMES} into which agent directories?",
                tuple(
                    (directory, "already present" if exists else "", exists)
                    for directory, exists in undecided
                ),
            )
        )
    chosen = {
        directory
        for directory, exists in undecided
        if exists
        and _ask_yes_no(
            _progress(
                progress,
                55,
                f"Install the {_SKILL_NAMES} into {directory}/skills? [Y/n]: ",
            ),
            True,
        )
    }
    absent = [directory for directory, exists in undecided if not exists]
    if absent:
        print("\nNo directory exists yet for these agents. ww can create one and")
        print("install its skill, so you can ask the agent to work through ww:")
        for row in _columns(absent, shutil.get_terminal_size((80, 24))[0] - 2):
            print("  " + row)
        print()
        chosen.update(
            _ask_names(
                _progress(progress, 90, "Create which? (comma-separated, or none): "),
                tuple(absent),
            )
        )
    return chosen


def _ask_directory(prompt: str) -> str | None:
    """Read a directory path, refusing an answer to the previous question.

    This prompt follows a ``[y/N]`` one, and a stray ``y`` accepted as the
    directory's name would create a directory called ``y`` and record it in
    the project configuration without complaint.
    """
    while True:
        value = input(prompt).strip()
        if not value:
            return None
        if value.lower() in {"y", "yes", "n", "no"}:
            print("That looks like an answer to the previous question.")
            print("Enter a directory path, or press Enter for ./git-worktrees.")
            continue
        return value


def _columns(names: list[str], width: int) -> list[str]:
    """Lay the names out in rows that fit, so a narrow terminal stays readable."""
    rows: list[str] = []
    row = ""
    for name in names:
        candidate = f"{row}  {name}" if row else name
        if row and len(candidate) > width:
            rows.append(row)
            row = name
        else:
            row = candidate
    if row:
        rows.append(row)
    return rows


def _progress(progress: bool, percent: int, question: str) -> str:
    return _init_prompt(percent, question) if progress else question


def _settings_by_level(storage: Storage) -> dict[str, dict[str, object]]:
    """Each settings level's own JSON object, by level name, where readable."""
    levels: dict[str, dict[str, object]] = {}
    for level in settings_levels(storage.project_config_path):
        if not level.path.is_file():
            continue
        try:
            raw = json.loads(level.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            levels[level.name] = raw
    return levels


def _configured_task_format(storage: Storage) -> bool:
    """Whether a settings level, user to local, already sets ``task_format``."""
    try:
        raw, _ = compose_settings(storage.project_config_path)
    except ConfigurationError:
        return False
    return bool(raw.get("task_format"))


def _existing_git_settings(storage: Storage) -> dict[str, object]:
    if not storage.project_config_path.is_file():
        return {}
    try:
        raw = json.loads(storage.project_config_path.read_text(encoding="utf-8"))
        extensions = raw.get("extensions", {})
        settings = extensions.get(GIT_EXTENSION, {})
    except (AttributeError, OSError, json.JSONDecodeError):
        return {}
    return settings if isinstance(settings, dict) else {}


def _workflow_names(storage: Storage) -> tuple[str, ...]:
    if not storage.config_path.is_file():
        return ()
    try:
        raw = compose_configuration(storage.config_path).raw
    except (OSError, ConfigurationError):
        return ()
    if not isinstance(raw.get("workflows"), list):
        return ()
    names: list[str] = []
    for item in raw["workflows"]:
        if isinstance(item, dict):
            name = item.get("name")
            if not isinstance(name, str) and len(item) == 1:
                name = next(iter(item))
            if isinstance(name, str):
                names.append(name)
    return tuple(names)


def _branch_format_arguments(values: list[str]) -> dict[str, str]:
    formats: dict[str, str] = {}
    for value in values:
        workflow, separator, branch_format = value.partition("=")
        if not separator or not workflow.strip() or not branch_format.strip():
            raise StateError("--branch-format must use WORKFLOW=FORMAT")
        formats[workflow.strip()] = branch_format.strip()
    return formats


def _git_base_branch(root: Path) -> str:
    for branch in ("master", "main"):
        reference_result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=root,
            check=False,
        )
        if reference_result.returncode == 0:
            return branch
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    current = result.stdout.strip()
    return current if current in {"master", "main"} else "main"


def _finish_initialization(
    storage: Storage,
    result: InitializationResult,
    *,
    shown: bool = True,
    force: bool = False,
) -> InitializationResult:
    """Complete the result; ``shown`` means the operator reads the summary.

    ``force`` shows the permission notice again even when it was shown.
    """
    created = list(result.created)
    actions = list(result.actions)
    try:
        raw = json.loads(storage.project_config_path.read_text(encoding="utf-8"))
        git = raw.get("extensions", {}).get(GIT_EXTENSION, {})
        configured = raw.get("executable")
    except (AttributeError, OSError, json.JSONDecodeError):
        git, configured = {}, None
    executable = (
        configured.strip()
        if isinstance(configured, str) and configured.strip()
        else DEFAULT_EXECUTABLE
    )
    command = project_command(executable, storage.root)
    if isinstance(git, dict) and git.get("worktrees") is True:
        configured = git.get("worktree_dir")
        if isinstance(configured, str) and configured:
            directory = Path(configured)
            if not directory.is_absolute():
                directory = storage.root / directory
            if not directory.exists():
                directory.mkdir(parents=True)
                try:
                    label = str(directory.relative_to(storage.root))
                except ValueError:
                    label = str(directory)
                created.append(label)
    path = storage.root / ".gitignore"
    ignored = path.is_file() and runtime_ignored(path.read_text(encoding="utf-8"))
    if not ignored and (storage.root / ".git").exists():
        actions.append("Optionally keep .ww out of Git with `init --update-gitignore`.")
    missing = [
        directory
        for directory in _agent_directories(storage)
        if not (storage.root / _skill_location(directory)).exists()
    ]
    if missing:
        actions.append(
            f"Optionally install the {_SKILL_NAMES} with `init --skills` "
            "for: " + ", ".join(missing) + "."
        )
    # The permission notice matters once: show it the first time the summary
    # is read, and remember that it was.
    notice = force or _init_choices(storage).get("permission_notice_shown") is not True
    if notice and shown:
        _save_init_choice(storage, "permission_notice_shown", True)
    commands = tuple(dict.fromkeys((command, PROJECT_LAUNCHER_COMMAND, "ww")))
    permissions, others = _agent_permissions(storage, commands)
    return replace(
        result,
        created=tuple(created),
        actions=tuple(actions),
        permission_notice=notice,
        executable=executable,
        command=command,
        commands=commands,
        permissions=permissions,
        other_agents=others,
    )


def _agent_permissions(
    storage: Storage, commands: tuple[str, ...]
) -> tuple[tuple[tuple[str, str, str], ...], tuple[str, ...]]:
    """What each agent set up here needs to run ``commands`` without asking.

    An agent counts as set up when its directory exists. For an agent whose
    permission format ww knows, the answer is its permissions file and the
    JSON to merge into it; every other agent is named for a generic line.
    """
    known: list[tuple[str, str, str]] = []
    others: list[str] = []
    for name, directory in AGENT_DIRECTORIES.items():
        if not (storage.root / directory).is_dir():
            continue
        agent = HOOK_AGENTS.get(name)
        entries = agent.permissions(commands) if agent else None
        if agent and agent.permissions_file and entries is not None:
            known.append((name, agent.permissions_file, json.dumps(entries, indent=2)))
        else:
            others.append(name)
    return tuple(known), tuple(others)
