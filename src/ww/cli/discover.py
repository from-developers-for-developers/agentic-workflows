# SPDX-License-Identifier: GPL-3.0-or-later
"""``discover``: everything an agent needs to choose and start a ww task.

The embeddable agent instructions stay short and send agents here, so the
choices shown always come from the project's current configuration.
"""

from __future__ import annotations

import json

from ww.builtin_workflows import CATCHALL, is_builtin
from ww.config import load_configuration, load_modes
from ww.contracts import CALLER_ROLES
from ww.discovery import AGENT_DIRECTORIES, CUSTOM_AGENT_PREFIX
from ww.executable import ww_command
from ww.extensions import ExtensionRegistry
from ww.hooks.notices import (
    RECENT_INTERRUPTION_DAYS,
    recent_interruptions_pointer,
)
from ww.hooks.records import HookRecords
from ww.instructions.commands import TASK_PLACEHOLDER, instruction_command
from ww.onboarding import Onboarding, OnboardingState
from ww.open_work import open_work
from ww.project_config import FILE_NAME, ON_REQUEST
from ww.runtimes import RUNTIME_DESCRIPTIONS
from ww.storage import Storage
from ww.task_ids import EXPLICIT_TASK_FORMAT
from ww.workflow_config import ALL_NAMES, delegation_requests

START_ARGUMENTS = (
    "start <TASK-ID> --workflow <workflow> --agent <agent> "
    '--requirements "<the user\'s requirements, normalized>" --role manager'
)
DISABLED_MESSAGE = (
    "Do not use ww for this work: do not start, continue, or complete ww "
    "tasks. Carry out the request without ww, and tell the user that ww is "
    f'disabled in {FILE_NAME} ("enabled": false).'
)
ON_REQUEST_MESSAGE = (
    "ww is used here only on request: use it only when the user explicitly "
    "asks for ww, such as by saying to use ww, naming a ww task, or invoking "
    "the `ww` skill; otherwise carry out the request without ww and do not ask."
)
ENABLED_MESSAGE = (
    "ww is enabled for this project. Choose the workflow that matches the "
    "request, and modes only when they apply, then start the task with the "
    "command below."
)
ON_REQUEST_CHOOSE = (
    "When the user has asked for ww, choose the workflow that matches the "
    "request, and modes only when they apply, then start the task with the "
    "command below."
)
ROLE_DESCRIPTIONS = {
    "manager": "Runs start and next, dispatches assignments, and handles recovery.",
    "worker": (
        "Performs one assignment and runs only the --role worker commands ww shows it."
    ),
}
MODEL_GUIDANCE = (
    "Optional. The model and reasoning level of your own session. Omit them "
    "(auto) unless you know them or the user asks for specific values; any "
    "value your agent understands is accepted as guidance."
)
TASK_ID_GUIDANCE = (
    "When the request names an external ticket, such as a Jira key, use that "
    "key as <TASK-ID> so the task matches the issue it works on. Omit <TASK-ID> "
    "only when the request names none, or when the workflow obtains its own ID "
    "in its first step; ww then assigns one."
)
EXPLICIT_ID_GUIDANCE = (
    "This project requires an explicit <TASK-ID>: use the external ticket key "
    "named in the request, such as a Jira key. Omit it only for a workflow "
    "that obtains its own ID in its first step; ww never generates one here."
)
RUNTIME_GUIDANCE = (
    "Choose deliberately rather than defaulting. Use `auto` when the workflow "
    "requests an agent, model, reasoning, or profile for any step — those "
    "requests only take effect when the manager delegates, and the list above "
    "marks the workflows that carry them. Use `single` when nothing is "
    "requested, when delegation is unavailable or not permitted, or when the "
    "user asked you to do the work yourself."
)
DELEGATION_NOTE = (
    "Steps requesting a specific worker are honoured only under `--runtime "
    "auto`; `single` records them and performs the step in this session."
)
CATCHALL_GUIDANCE = (
    "Use it only when no workflow above fits and you are about to change "
    "files. Questions, explanations, reviews, and other read-only work need "
    "no task: answer them directly, and turn to it only once the conversation "
    "reaches a change. Do not start it directly. Run `lookup` with the task "
    "this conversation works on, or with what the operator called the task, "
    "as they wrote it, such as `12345`; run it without one when there is "
    "none. It maps the reference onto this project's task IDs and answers "
    "with the next step: continue an unfinished run, start the catch-all on "
    "the task it found, or ask the operator, through your choice menu, before "
    "a task ww has never seen is created."
)
BUILTIN_GUIDANCE = (
    "ww ships these to learn about the operator and the project and to set "
    "ww up; start one like any workflow when the operator asks for what it "
    "does, or when a ww skill says to."
)
# Under ``"on_request"`` an unasked change never reaches the catch-all.
ON_REQUEST_CATCHALL_PREFIX = (
    "Only when the user has asked for ww; otherwise make the change without "
    "ww. "
)
SETUP_GUIDANCE = (
    "This is the first use of ww in this project: it has not been set up here. "
    "Offer the operator the `ww-setup` skill, which walks them through setting "
    "ww up, by asking through your choice menu; do not start it unasked."
)
EXPLAIN_GUIDANCE = (
    "The operator has not said whether they want to see what ww does while it "
    "learns. Ask them once, through your choice menu, and record the answer "
    "with `{command} onboarding --set explain=true` (or `explain=false`)."
)
ON_REQUEST_SETUP_NOTE = (
    "For information: ww has not been set up in this project yet; when the "
    "operator asks for ww, the `ww-setup` skill can walk them through it."
)
ON_REQUEST_EXPLAIN_NOTE = (
    "For information: the operator has not said whether they want to see what "
    "ww does while it learns; `{command} onboarding --set explain=true` (or "
    "`explain=false`) records it once they say."
)
UNREADABLE_GUIDANCE = (
    "Other tasks and new work are unaffected. Commands addressing these tasks "
    "fail with the error shown; ask the operator, whose choice it is to repair, "
    "reset, or delete each task directory."
)
MODES_GUIDANCE = (
    "Optional and repeatable. Explicit modes replace the workflow's default "
    "modes, so repeat any default you want to keep. Select a mode only when "
    "the user's request matches its description. A mode marked always on "
    "applies by itself where it says; never select it."
)


def discover(storage: Storage, extensions: ExtensionRegistry) -> dict[str, object]:
    """Collect the choices and commands for starting a task in this project."""
    config = extensions.config
    if config.disabled:
        return {"enabled": False, "message": DISABLED_MESSAGE}
    configuration = load_configuration(storage.config_path, extensions)
    explicit_ids = extensions.task_format() == EXPLICIT_TASK_FORMAT
    default_runtime = extensions.config.runtime
    modes = load_modes(storage.config_path, extensions)
    modes.update({mode.name: mode for mode in extensions.qualified_modes()})
    catchall = configuration.workflows_by_name.get(CATCHALL)
    onboarding = Onboarding(storage.root, storage.project_metadata).read()
    return {
        # ``"on_request"`` still lists everything, so an explicit request can
        # proceed; the header tells an agent not to use ww unasked.
        "enabled": ON_REQUEST if config.on_request else True,
        "onboarding": {
            "setup_done": onboarding.setup_done,
            "explain": onboarding.explain,
            "guidance": _onboarding_guidance(onboarding, on_request=config.on_request),
        },
        "projects": [
            {
                **project.to_dict(),
                "branch_strategies": list(extensions.branch_strategies(project.name)),
                # The project's own task ID format; null when the root's applies.
                "task_format": extensions.project_settings(project.name).task_format,
            }
            for project in extensions.config.projects
        ],
        "workflows": [
            {
                "name": workflow.name,
                "description": workflow.description,
                "default_modes": list(workflow.modes),
                "runtime": workflow.runtime,
                "inherits": workflow.inherits,
                "recommended_next_workflow": workflow.recommended_next_workflow,
                "delegation_requests": list(delegation_requests(workflow)),
            }
            for workflow in configuration.workflows
            if workflow is not catchall and not is_builtin(workflow)
        ],
        # ww's own workflows, such as its learning ones; the catch-all is
        # listed apart below.
        "builtin_workflows": [
            {"name": workflow.name, "description": workflow.description}
            for workflow in configuration.workflows
            if workflow is not catchall and is_builtin(workflow)
        ],
        "catchall": (
            {
                "name": catchall.name,
                "description": catchall.description,
                "guidance": (
                    ON_REQUEST_CATCHALL_PREFIX + CATCHALL_GUIDANCE
                    if config.on_request
                    else CATCHALL_GUIDANCE
                ),
                "start": f"{ww_command()} lookup [<task>] --agent <agent>",
            }
            if catchall is not None
            else None
        ),
        "modes": [
            {
                "name": mode.name,
                "description": " ".join(mode.description),
                "automatic": (
                    {
                        key: value.to_data()
                        for key, value in (
                            ("workflows", mode.workflows),
                            ("steps", mode.steps),
                        )
                        if value is not None
                    }
                    if mode.automatic
                    else None
                ),
            }
            for mode in modes.values()
        ],
        "runtimes": [
            {
                "name": name,
                "description": description,
                "default": name == default_runtime,
            }
            for name, description in RUNTIME_DESCRIPTIONS.items()
        ],
        "roles": [
            {"name": role, "description": ROLE_DESCRIPTIONS[role]}
            for role in CALLER_ROLES
        ],
        "agents": [*AGENT_DIRECTORIES, f"{CUSTOM_AGENT_PREFIX}<name>"],
        "branch_strategies": list(extensions.branch_strategies()),
        "model_and_reasoning": MODEL_GUIDANCE,
        "runtime_guidance": RUNTIME_GUIDANCE,
        "task_id": (EXPLICIT_ID_GUIDANCE if explicit_ids else TASK_ID_GUIDANCE),
        "modes_guidance": MODES_GUIDANCE,
        "commands": {
            "start": f"{ww_command()} {START_ARGUMENTS}",
            "instruction": instruction_command(TASK_PLACEHOLDER, role="manager"),
            "status": f"{ww_command()} status {TASK_PLACEHOLDER}",
            "plan": f"{ww_command()} plan --workflow <workflow> --agent <agent>",
        },
    }


def render_discover(
    storage: Storage, extensions: ExtensionRegistry, json_output: bool
) -> str:
    report = discover(storage, extensions)
    if report["enabled"]:
        report["interrupted_recently"] = _recent_interruptions(storage)
        report["unreadable_tasks"] = [
            task.to_dict()
            for task in open_work(storage.task_persistence, storage.root).unreadable
        ]
    if json_output:
        return json.dumps(report, indent=2)
    if not report["enabled"]:
        return "\n".join(
            [
                "# ww discover",
                "",
                "**ww is disabled for this project.**",
                "",
                DISABLED_MESSAGE,
            ]
        )
    return "\n".join(_markdown(report))


def _mode_line(mode: dict[str, object]) -> str:
    """One catalog mode, with where it applies by itself when it is automatic."""
    line = f"- `{mode['name']}` — {mode['description']}"
    automatic = mode.get("automatic")
    if isinstance(automatic, dict):
        places = "; ".join(
            f"{key} "
            + (
                "all"
                if value == ALL_NAMES
                else ", ".join(f"`{name}`" for name in _strings(value))
            )
            for key, value in automatic.items()
        )
        line += f" Always on: {places}."
    return line


def _markdown(report: dict[str, object]) -> list[str]:
    workflows = _entries(report["workflows"])
    modes = _entries(report["modes"])
    runtimes = _entries(report["runtimes"])
    roles = _entries(report["roles"])
    *named, custom = (f"`{agent}`" for agent in _strings(report["agents"]))
    agents = ", ".join(named) + f", or {custom}"
    strategies = _strings(report["branch_strategies"])
    commands = report["commands"]
    assert isinstance(commands, dict)
    lines = [
        "# ww discover",
        "",
        *(
            [f"**{ON_REQUEST_MESSAGE}**", "", ON_REQUEST_CHOOSE]
            if report["enabled"] == ON_REQUEST
            else [ENABLED_MESSAGE]
        ),
        "",
        *_pointer_lines(report),
        *_unreadable_lines(report),
        *_onboarding_lines(report),
        "## Workflows",
        "",
    ]
    for workflow in workflows:
        text = (
            f"- `{workflow['name']}` — {workflow['description'] or 'No description.'}"
        )
        defaults = _strings(workflow["default_modes"])
        if defaults:
            text += " Default modes: " + ", ".join(f"`{m}`" for m in defaults) + "."
        if workflow.get("runtime"):
            text += f" Runtime: `{workflow['runtime']}`."
        if workflow.get("inherits"):
            text += f" Same steps as `{workflow['inherits']}`."
        if workflow.get("recommended_next_workflow"):
            text += (
                f" Offers `{workflow['recommended_next_workflow']}` next, "
                "on the operator's confirmation."
            )
        requests = _strings(workflow.get("delegation_requests", []))
        if requests:
            text += (
                " Requests a specific worker on: "
                + ", ".join(f"`{name}`" for name in requests)
                + " — start it with `--runtime auto` so those requests apply."
            )
        lines.append(text)
    catchall = report["catchall"]
    if not workflows and not catchall and not report["builtin_workflows"]:
        lines.append("No workflows are configured; ww cannot start a task.")
    if isinstance(catchall, dict):
        lines.extend(
            [
                "",
                "## Changes no workflow covers",
                "",
                f"- `{catchall['name']}` — {catchall['description']}",
                "",
                str(catchall["guidance"]),
                "",
                "```console",
                str(catchall["start"]),
                "```",
            ]
        )
    builtins = _entries(report["builtin_workflows"])
    if builtins:
        lines.extend(["", "## ww's own workflows", ""])
        lines.extend(
            f"- `{workflow['name']}` — {workflow['description']}"
            for workflow in builtins
        )
        lines.extend(["", BUILTIN_GUIDANCE])
    projects = _entries(report["projects"])
    if projects:
        lines.extend(["", "## Projects", ""])
        for project in projects:
            line = f"- `{project['name']}` at `{project['path']}`"
            if project["description"]:
                line += f" — {project['description']}"
            project_strategies = _strings(project.get("branch_strategies", []))
            if project_strategies != strategies:
                line += " Branch strategies there: " + (
                    ", ".join(f"`{name}`" for name in project_strategies) or "none"
                ) + "."
            task_format = project.get("task_format")
            if isinstance(task_format, str):
                line += (
                    " Tasks there require an explicit ID."
                    if task_format == EXPLICIT_TASK_FORMAT
                    else f" Generated task IDs there follow `{task_format}`."
                )
            lines.append(line)
        lines.append(
            "A task works in one project directory when started with "
            "`--project <name>`; without it, the task works in the root. A "
            f"project's own `{FILE_NAME}` may carry an `extensions` section, "
            "which applies over the root's for work done in that project, and "
            "a `task_format` of its own."
        )
    lines.extend(["", "## Modes", ""])
    lines.extend(_mode_line(mode) for mode in modes)
    if not modes:
        lines.append("No modes are configured.")
    lines.extend(["", "## Runtimes", ""])
    lines.extend(
        f"- `{runtime['name']}`{' (project default)' if runtime['default'] else ''} — "
        f"{runtime['description']}"
        for runtime in runtimes
    )
    lines.extend(["", str(report["runtime_guidance"])])
    lines.extend(["", "## Roles", ""])
    lines.extend(f"- `{role['name']}` — {role['description']}" for role in roles)
    lines.extend(
        [
            "",
            "## Start options",
            "",
            "- `--workflow` (`-w`): one workflow name from the list above.",
            f"- `--agent` (`-a`): your agent integration: {agents}.",
            f"- `--mode`: {report['modes_guidance']}",
            "- `--runtime` (`-r`): "
            + " or ".join(f"`{runtime['name']}`" for runtime in runtimes)
            + f". {DELEGATION_NOTE} Omitted, ww uses the workflow's own "
            "runtime if it declares one, then the project default, "
            + next(f"`{runtime['name']}`" for runtime in runtimes if runtime["default"])
            + ".",
            f"- `--model`, `--reasoning`: {report['model_and_reasoning']}",
            *(
                [
                    "- `--project`: "
                    + ", ".join(f"`{project['name']}`" for project in projects)
                    + ". Omit it to work in the root."
                ]
                if projects
                else []
            ),
            "- `--branch-strategy`: "
            + (
                ", ".join(f"`{name}`" for name in strategies)
                + ". Omit it to use the workflow's own branch format."
                if strategies
                else "no extension defines branch strategies here; omit it."
            ),
            "- `--role`: `manager` for `start`.",
            "",
            "## Commands",
            "",
            "To start a task:",
            "",
            "```console",
            str(commands["start"]),
            "```",
            "",
            str(report["task_id"]),
            "",
            "To show the instructions for an existing task:",
            "",
            "```console",
            str(commands["instruction"]),
            "```",
            "",
            "To see a task's status:",
            "",
            "```console",
            str(commands["status"]),
            "```",
            "",
            "To inspect a workflow's steps before starting:",
            "",
            "```console",
            str(commands["plan"]),
            "```",
            "",
            "After `start`, follow each ww response exactly until ww reports that "
            "the workflow is complete.",
        ]
    )
    return lines


def _entries(value: object) -> list[dict[str, object]]:
    return (
        [entry for entry in value if isinstance(entry, dict)]
        if isinstance(value, list)
        else []
    )


def _strings(value: object) -> list[str]:
    return [str(entry) for entry in value] if isinstance(value, list) else []


def _recent_interruptions(storage: Storage) -> int:
    return len(
        HookRecords(storage, storage.task_persistence).recent(RECENT_INTERRUPTION_DAYS)
    )


def _unreadable_lines(report: dict[str, object]) -> list[str]:
    tasks = _entries(report.get("unreadable_tasks", []))
    if not tasks:
        return []
    return [
        "## Unreadable tasks",
        "",
        *(f"- `{task['task_id']}` — {task['reason']}" for task in tasks),
        "",
        UNREADABLE_GUIDANCE,
        "",
    ]


def _pointer_lines(report: dict[str, object]) -> list[str]:
    count = report.get("interrupted_recently")
    pointer = recent_interruptions_pointer(count if isinstance(count, int) else 0)
    return [pointer, ""] if pointer else []


def _onboarding_guidance(state: OnboardingState, *, on_request: bool) -> list[str]:
    """What to offer on a first use; under ``"on_request"``, information only."""
    command = ww_command()
    guidance = []
    if not state.setup_done:
        guidance.append(ON_REQUEST_SETUP_NOTE if on_request else SETUP_GUIDANCE)
    if state.explain is None:
        guidance.append(
            (ON_REQUEST_EXPLAIN_NOTE if on_request else EXPLAIN_GUIDANCE).format(
                command=command
            )
        )
    return guidance


def _onboarding_lines(report: dict[str, object]) -> list[str]:
    onboarding = report.get("onboarding")
    guidance = _strings(onboarding.get("guidance")) if isinstance(
        onboarding, dict
    ) else []
    if not guidance:
        return []
    return ["## Onboarding", "", *(f"- {line}" for line in guidance), ""]
