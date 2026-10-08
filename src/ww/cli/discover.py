# SPDX-License-Identifier: GPL-3.0-or-later
"""``discover``: everything an agent needs to choose and start a ww task.

The embeddable agent instructions stay short and send agents here, so the
choices shown always come from the project's current configuration.
"""

from __future__ import annotations

import json
from pathlib import Path

from ww.builtin_workflows import is_builtin
from ww.config import load_configuration, load_modes
from ww.contracts import CALLER_ROLES
from ww.discovery import AGENT_DIRECTORIES, CUSTOM_AGENT_PREFIX
from ww.errors import StateError
from ww.executable import ww_command
from ww.extensions import ExtensionRegistry
from ww.instructions.commands import (
    TASK_PLACEHOLDER,
    instruction_command,
    record_command,
)
from ww.onboarding import Onboarding, OnboardingState
from ww.open_work import unreadable_tasks
from ww.project_config import FILE_NAME, ON_REQUEST
from ww.rule_conversion import scriptize_notice
from ww.rule_store import RuleStore
from ww.runtimes import RUNTIME_DESCRIPTIONS
from ww.storage import Storage
from ww.task_ids import EXPLICIT_TASK_FORMAT
from ww.workflow_config import (
    ALL_NAMES,
    WorkflowConfiguration,
    delegation_requests,
)

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
ROLE_DESCRIPTIONS = {
    "manager": "Runs start and next, dispatches assignments, and handles recovery.",
    "worker": (
        "Performs one assignment and runs only the --role worker commands ww shows it."
    ),
}
# Under ``"on_request"`` an unasked change never reaches direct work.
ON_REQUEST_DIRECT_WORK_PREFIX = (
    "Only when the user has asked for ww; otherwise make the change without ww. "
)
SETUP_GUIDANCE = (
    "ww has not been set up in this project yet. Setup is optional and never "
    "blocks ordinary work: carry on with the request, and mention the "
    "`ww-setup` skill only if the operator asks to set ww up or asks what ww "
    "can do here."
)
ON_REQUEST_SETUP_NOTE = (
    "For information: ww has not been set up in this project yet; when the "
    "operator asks for ww, the `ww-setup` skill can set it up."
)
UNREADABLE_GUIDANCE = (
    "Other tasks and new work are unaffected. Commands addressing these tasks "
    "fail with the error shown; ask the operator, whose choice it is to repair, "
    "reset, or delete each task directory."
)

ENABLED_SHORT = "ww is enabled for this project."
ON_REQUEST_START = (
    "When the user has asked for ww, choose a workflow and start a task as below."
)
SELECTION_GUIDANCE = (
    "Choose a workflow matching the request. When multiple workflows fit, prefer "
    "local over project over global. Honor an explicitly requested workflow. If "
    "the choice remains unclear, ask the operator."
)
BUILTIN_POINTER = (
    "ww's own workflows (setup, learning, rule automation) are not listed here: "
    "`{command} workflows` lists every workflow. Start one when the operator asks "
    "for what it does or a ww skill says to."
)
DIRECT_WORK_GUIDANCE = (
    "A request that changes files and names a ticket, or clearly matches a "
    "workflow above, and is more than a small change: propose that workflow, "
    'and offer the alternative in the same choice, "run `<workflow>`" or '
    '"just do it, register afterwards". Any other request: work directly, as '
    "in a plain conversation, and ask nothing; when done, register it. Judge "
    "each prompt on its own: a series of small requests stays a series of "
    "direct-work entries, never a workflow because they add up. Ask only "
    "when it is genuinely ambiguous, and never again on a task once the "
    "operator chose direct work. Read-only work needs no task. If `record` "
    "fails or is forgotten, ww records the commits it had not seen on the "
    "next run; do not retry it endlessly."
)
PROJECTS_GUIDANCE = (
    "`--project <name>` works in that project's directory; without it the task "
    "works in the root. A project's own `ww.json` may add `extensions` and a "
    "`task_format` that apply there."
)
TASK_ID_GUIDANCE = (
    "When the request names an external ticket, such as a Jira key, use it as "
    "the task ID so the task matches the issue. Omit the task ID only when the "
    "request names none, or when the workflow obtains its own in its first "
    "step; ww then assigns one."
)
EXPLICIT_ID_GUIDANCE = (
    "This project requires an explicit task ID: use the external ticket key "
    "named in the request. Omit it only for a workflow that obtains its own ID "
    "in its first step; ww never generates one here."
)
MODES_GUIDANCE = (
    "Explicit `--mode` values replace the workflow's default modes, so repeat "
    "any default you want to keep. Select a mode only when the request matches "
    "its description; a mode marked always on applies by itself."
)
RUNTIME_GUIDANCE = (
    "Use `auto` when the workflow requests specific workers (marked above); "
    "`single` when nothing is requested, delegation is unavailable, or the user "
    "asked you to do the work yourself. Omitted, the workflow's own runtime "
    "applies, then the project default `{default}`."
)
MODEL_GUIDANCE = (
    "`--model` and `--reasoning` describe your own session; omit them unless "
    "you know them or the user asks."
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
        # Declared rules no check covers yet; never a reason not to start.
        "rules_notice": _rules_notice(configuration, storage.root),
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
                **_workflow_source_fields(configuration, workflow.name),
                "default_modes": list(workflow.modes),
                "runtime": workflow.runtime,
                "inherits": workflow.inherits,
                "recommended_next_workflow": workflow.recommended_next_workflow,
                "delegation_requests": list(delegation_requests(workflow)),
            }
            for workflow in configuration.workflows
            if not workflow.manual and not is_builtin(workflow)
        ],
        # ww's own workflows, such as its learning ones.
        "builtin_workflows": [
            {
                "name": workflow.name,
                "description": workflow.description,
                **_workflow_source_fields(configuration, workflow.name),
            }
            for workflow in configuration.workflows
            if not workflow.manual and is_builtin(workflow)
        ],
        "direct_work": {
            "guidance": (
                ON_REQUEST_DIRECT_WORK_PREFIX + DIRECT_WORK_GUIDANCE
                if config.on_request
                else DIRECT_WORK_GUIDANCE
            ),
            "record": record_command(),
            "lookup": f"{ww_command()} lookup [<task>] --agent <agent>",
        },
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
        "runtime_guidance": RUNTIME_GUIDANCE.format(default=default_runtime),
        "task_id": (EXPLICIT_ID_GUIDANCE if explicit_ids else TASK_ID_GUIDANCE),
        # Whether the project requires the task ID; the guidance above is prose.
        "explicit_task_id": explicit_ids,
        "modes_guidance": MODES_GUIDANCE,
        "commands": {
            "start": f"{ww_command()} {START_ARGUMENTS}",
            "instruction": instruction_command(TASK_PLACEHOLDER, role="manager"),
            "status": f"{ww_command()} status {TASK_PLACEHOLDER}",
            "plan": f"{ww_command()} plan --workflow <workflow> --agent <agent>",
        },
    }


def _workflow_source_fields(
    configuration: WorkflowConfiguration, name: str
) -> dict[str, str | None]:
    """Additive JSON provenance; null means a non-configured workflow source."""
    provenance = configuration.workflow_provenance.get(name)
    return {
        "source": provenance.source if provenance is not None else None,
        "source_level": provenance.level if provenance is not None else None,
    }


def render_discover(
    storage: Storage, extensions: ExtensionRegistry, json_output: bool
) -> str:
    report = discover(storage, extensions)
    if report["enabled"]:
        report["unreadable_tasks"] = [
            task.to_dict() for task in unreadable_tasks(storage.task_persistence)
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
    # Preference order is a display concern; the JSON keeps declaration order.
    workflows = sorted(_entries(report["workflows"]), key=_level_rank)
    modes = _entries(report["modes"])
    runtimes = _entries(report["runtimes"])
    projects = _entries(report["projects"])
    strategies = _strings(report["branch_strategies"])
    commands = report["commands"]
    assert isinstance(commands, dict)
    on_request = report["enabled"] == ON_REQUEST
    lines = [
        "# ww discover",
        "",
        *(
            [f"**{ON_REQUEST_MESSAGE}**", "", ON_REQUEST_START]
            if on_request
            else [ENABLED_SHORT]
        ),
        "",
        *_unreadable_lines(report),
        *_onboarding_lines(report),
        "## Workflows",
        "",
    ]
    if workflows:
        lines.extend([SELECTION_GUIDANCE, ""])
        lines.extend(_workflow_line(workflow) for workflow in workflows)
    if not workflows and not report["builtin_workflows"]:
        lines.append("No workflows are configured; ww cannot start a task.")
    if report["builtin_workflows"]:
        lines.extend(["", BUILTIN_POINTER.format(command=ww_command())])
    direct_work = report["direct_work"]
    assert isinstance(direct_work, dict)
    lines.extend(
        [
            "",
            "## Direct work",
            "",
            str(direct_work["guidance"]),
            "",
            "```console",
            str(direct_work["lookup"]),
            str(direct_work["record"]),
            "```",
        ]
    )
    if projects:
        lines.extend(["", "## Projects", ""])
        lines.extend(_project_line(project, strategies) for project in projects)
        lines.extend(["", PROJECTS_GUIDANCE])
    if modes:
        lines.extend(["", "## Modes", ""])
        lines.extend(_mode_line(mode) for mode in modes)
    lines.extend(
        [
            "",
            "## Start a task",
            "",
            "Square brackets denote optional arguments.",
            "",
            "```console",
            *_start_synopsis(report, runtimes, projects, strategies),
            "```",
            "",
            str(report["task_id"]),
            "",
            *_start_notes(report, runtimes),
            "",
            "To continue a task, or to check one, and optionally to preview a "
            "workflow's plan:",
            "",
            "```console",
            str(commands["instruction"]),
            str(commands["status"]),
            str(commands["plan"]),
            "```",
            "",
            "After `start`, follow each ww response exactly until ww reports that "
            "the workflow is complete.",
        ]
    )
    return lines


LEVEL_ORDER = {"local": 0, "project": 1, "global": 2}


def _level_rank(workflow: dict[str, object]) -> int:
    """Where a workflow sorts: local, project, global, then any other source."""
    return LEVEL_ORDER.get(str(workflow.get("source_level")), len(LEVEL_ORDER))


def _display_source(source: str) -> str:
    """A source path as an operator reads it: the home directory is ``~``."""
    try:
        return "~/" + Path(source).relative_to(Path.home()).as_posix()
    except ValueError:
        return source


def _workflow_label(workflow: dict[str, object]) -> str:
    level, source = workflow.get("source_level"), workflow.get("source")
    if isinstance(level, str) and isinstance(source, str):
        return f"[{level}: {_display_source(source)}]"
    return "[other: not from a configuration file]"


def _workflow_line(workflow: dict[str, object]) -> str:
    text = (
        f"- {workflow['name']} — {_workflow_label(workflow)} "
        f"{workflow['description'] or 'No description.'}"
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
            " Requests specific workers on "
            + ", ".join(f"`{name}`" for name in requests)
            + "; use `--runtime auto`."
        )
    return text


def _project_line(project: dict[str, object], strategies: list[str]) -> str:
    line = f"- `{project['name']}` at `{project['path']}`"
    if project["description"]:
        line += f" — {project['description']}"
    project_strategies = _strings(project.get("branch_strategies", []))
    if project_strategies != strategies:
        line += (
            " Branch strategies there: "
            + (", ".join(f"`{name}`" for name in project_strategies) or "none")
            + "."
        )
    task_format = project.get("task_format")
    if isinstance(task_format, str):
        line += (
            " Tasks there require an explicit ID."
            if task_format == EXPLICIT_TASK_FORMAT
            else f" Generated task IDs there follow `{task_format}`."
        )
    return line


def _start_synopsis(
    report: dict[str, object],
    runtimes: list[dict[str, object]],
    projects: list[dict[str, object]],
    strategies: list[str],
) -> list[str]:
    task_id = "<task-id>" if report["explicit_task_id"] else "[<task-id>]"
    runtime_names = "|".join(str(runtime["name"]) for runtime in runtimes)
    optional = [
        "[--mode <mode>]",
        f"[--runtime <{runtime_names}>]",
        "[--model <model>]",
        "[--reasoning <level>]",
    ]
    if projects:
        names = "|".join(str(project["name"]) for project in projects)
        optional.append(f"[--project <{names}>]")
    if strategies:
        optional.append(f"[--branch-strategy <{'|'.join(strategies)}>]")
    indent = "  "
    return [
        f"{ww_command()} start {task_id} --workflow <workflow> --agent <agent> \\",
        f'{indent}--requirements "<the user\'s requirements, normalized>" \\',
        *(f"{indent}{option} \\" for option in optional),
        f"{indent}--role manager",
    ]


def _start_notes(
    report: dict[str, object], runtimes: list[dict[str, object]]
) -> list[str]:
    *named, custom = (f"`{agent}`" for agent in _strings(report["agents"]))
    return [
        f"- Agent: {', '.join(named)}, or {custom}.",
        f"- {report['modes_guidance']}",
        "- Runtimes: "
        + "; ".join(
            f"`{runtime['name']}`"
            + (" (project default)" if runtime["default"] else "")
            + f" — {str(runtime['description']).rstrip('.')}"
            for runtime in runtimes
        )
        + f". {report['runtime_guidance']}",
        f"- {report['model_and_reasoning']}",
    ]


def _entries(value: object) -> list[dict[str, object]]:
    return (
        [entry for entry in value if isinstance(entry, dict)]
        if isinstance(value, list)
        else []
    )


def _strings(value: object) -> list[str]:
    return [str(entry) for entry in value] if isinstance(value, list) else []


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


def _onboarding_guidance(state: OnboardingState, *, on_request: bool) -> list[str]:
    """What to offer on a first use; under ``"on_request"``, information only."""
    if state.setup_done:
        return []
    return [ON_REQUEST_SETUP_NOTE if on_request else SETUP_GUIDANCE]


def _onboarding_lines(report: dict[str, object]) -> list[str]:
    onboarding = report.get("onboarding")
    guidance = (
        _strings(onboarding.get("guidance")) if isinstance(onboarding, dict) else []
    )
    if not guidance:
        return []
    return ["## Onboarding", "", *(f"- {line}" for line in guidance), ""]


def _rules_notice(configuration: WorkflowConfiguration, root: Path) -> str | None:
    """The unscriptized-rules notice, or none when the store cannot be read.

    ``discover`` is the first command an agent runs; a malformed store is
    reported by ``lint`` and by the commands that use it, never here.
    """
    try:
        automation = RuleStore(root).load()
    except StateError:
        return None
    return scriptize_notice(configuration, automation)
