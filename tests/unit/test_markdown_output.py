# SPDX-License-Identifier: GPL-3.0-or-later
from ww.children import ChildTask
from ww.instructions import Instruction
from ww.instructions.commands import recovery_commands
from ww.output_adapters.markdown import MarkdownOutputAdapter
from ww.workflow_config import ProvidedVariable


def test_working_directory_precedes_the_work_without_extra_spacing() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="in_progress",
        item_id="item-1",
        item_name="update-docs",
        stage="Completion hook",
        step="work",
        parent=None,
        item_status="in_progress",
        action_kind="skill",
        action_text="Use the `update-architecture-documentation` skill.",
        working_directory="/tmp/task-worktree",
        workflow_runtime="single",
        caller_role="worker",
        next_role="worker",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    working = rendered.index("### Working directory")
    work = rendered.index("### Work instruction")
    assert working < work
    assert "\n\n\n### Work instruction" not in rendered
    assert "Use the `update-architecture-documentation` skill." in rendered


def test_error_heading_has_normalized_section_spacing() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="failed",
        item_id="item-1",
        item_name="check",
        stage="Completion hook",
        step="work",
        parent=None,
        item_status="failed",
        action_kind="cli",
        action_text=None,
        error="The check failed.",
        workflow_runtime="single",
        caller_role="worker",
        next_role="manager",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "\n\n### Error\n\nThe check failed." in rendered
    assert "\n\n\n### Error" not in rendered


def test_failed_handler_directs_a_worker_to_report_to_the_manager() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="failed",
        item_id="item-1",
        item_name="git-is-clean",
        stage="Before Start hook",
        step="init",
        parent=None,
        item_status="failed",
        action_kind="cli",
        action_text=None,
        error="The Git working tree is not clean.",
        workflow_runtime="auto",
        caller_role="worker",
        next_role="manager",
        continuation_command="./ww next TASK-1 --role manager",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "### Retry" not in rendered
    assert "./ww next TASK-1 --role manager" not in rendered
    assert "Return this `ww` response to the manager for resolution." in rendered


def test_failed_handler_directs_the_manager_to_an_operator() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="failed",
        item_id="item-1",
        item_name="git-is-clean",
        stage="Before Start hook",
        step="init",
        parent=None,
        item_status="failed",
        action_kind="cli",
        action_text=None,
        error="The Git working tree is not clean.",
        workflow_runtime="single",
        caller_role="worker",
        next_role="manager",
        recovery_commands=recovery_commands("TASK-1"),
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "Do not retry on your own" in rendered
    assert "### Retry" not in rendered
    assert "### Operator recovery" in rendered
    assert "./ww next TASK-1 --retry --yes --role manager" in rendered
    assert '--force --reason "<reason>" --yes' in rendered


def test_manager_command_has_normalized_section_spacing() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="in_progress",
        item_id="item-1",
        item_name="implementation",
        stage="Workflow step",
        step="implementation",
        parent=None,
        item_status="pending",
        action_kind="prompt",
        action_text=None,
        continuation_command="./ww next TASK-1 --role manager",
        workflow_runtime="single",
        caller_role="manager",
        next_role="worker",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "\n\n### Manager command\n\n" in rendered
    assert "\n\n\n### Manager command" not in rendered


def test_orchestrate_a_manager_step_keeps_work_with_the_manager() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="in_progress",
        item_id="item-1",
        item_name="local-work",
        stage="Workflow step",
        step="local-work",
        parent=None,
        item_status="in_progress",
        action_kind="prompt",
        action_text="Do this locally.",
        workflow_runtime="auto",
        workflow_runtime_instruction=("Do not delegate this step.",),
        caller_role="manager",
        next_role="worker",
        role="manager",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "### Worker bootstrap" not in rendered
    assert "Do this locally." in rendered


def test_previous_artifacts_are_available_through_the_json_command() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="in_progress",
        item_id="item-2",
        item_name="develop",
        stage="Workflow step",
        step="develop",
        parent=None,
        item_status="in_progress",
        action_kind="prompt",
        action_text="Implement the change.",
        has_previous_artifacts=True,
        run_id="01-task",
        workflow_runtime="single",
        caller_role="worker",
        next_role="worker",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "### Previous artifacts" in rendered
    assert "./ww artifacts TASK-1 --run 01-task" in rendered
    assert "steps/" not in rendered


def test_next_steps_warn_against_duplicate_work_and_list_only_names() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="in_progress",
        item_id="item-2",
        item_name="develop",
        stage="Workflow step",
        step="develop",
        parent=None,
        item_status="in_progress",
        action_kind="prompt",
        action_text="Develop the change.",
        next_steps=("run-tests", "check-code-quality"),
        workflow_runtime="single",
        caller_role="worker",
        next_role="worker",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert (
        "### Next steps\n\n"
        "Leave to them the work they cover:\n\n"
        "- run-tests\n"
        "- check-code-quality"
    ) in rendered


def test_completion_acknowledgement_is_adjacent_and_omitted_on_error() -> None:
    successful = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="completed",
        item_id=None,
        item_name=None,
        stage=None,
        step=None,
        parent=None,
        item_status=None,
        action_kind=None,
        action_text=None,
        completion_registered=True,
    )
    failed = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="failed",
        item_id="item-1",
        item_name="check",
        stage="Before Start hook",
        step="init",
        parent=None,
        item_status="failed",
        action_kind="cli",
        action_text=None,
        error="The check failed.",
        completion_registered=True,
    )

    successful_rendered = MarkdownOutputAdapter().render_instruction(successful)
    failed_rendered = MarkdownOutputAdapter().render_instruction(failed)

    assert (
        "current task state.\n> Completion recorded successfully by `ww`."
        in successful_rendered
    )
    assert "Completion recorded successfully" not in failed_rendered


def test_failed_child_includes_its_operator_recovery_command() -> None:
    instruction = Instruction(
        task_id="TASK-40",
        workflow="parent",
        status="failed",
        item_id="item-2",
        item_name="execute-children",
        stage="Workflow step",
        step="execute-children",
        parent=None,
        item_status="failed",
        action_kind="child_workflow",
        action_text=None,
        error="child '2' failed",
        child_tasks=(
            ChildTask(
                id="2",
                description="Implement the child task.",
                workflow="child",
                task_id="TASK-40/2",
                status="failed",
            ),
        ),
        workflow_runtime="single",
        caller_role="manager",
        next_role="manager",
        is_child_workflow_control=True,
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "### Child task recovery" in rendered
    assert "./ww next TASK-40/2 --role manager" in rendered
    assert "resumes the parent workflow automatically" in rendered


def test_task_id_completion_explains_how_to_replace_its_placeholder() -> None:
    instruction = Instruction(
        task_id="REQUEST-1",
        workflow="feature",
        status="in_progress",
        item_id="item-1",
        item_name="create-jira",
        stage="Workflow step",
        step="create-jira",
        parent=None,
        item_status="in_progress",
        action_kind="mcp",
        action_text="Create the Jira issue.",
        required_values=(ProvidedVariable("task_id", "The external task ID."),),
        continuation_command=(
            './ww complete REQUEST-1 --role worker --variable task_id="<task_id>"'
        ),
        workflow_runtime="single",
        caller_role="worker",
        next_role="worker",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "Values to supply in place of their `<...>` placeholders" in rendered
    assert "do not submit the literal `<task_id>` placeholder" in rendered


def test_orchestrate_manager_delegates_a_worker_bootstrap_command() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="in_progress",
        item_id="item-1",
        item_name="develop",
        stage="Workflow step",
        step="develop",
        parent=None,
        item_status="in_progress",
        action_kind="prompt",
        action_text="Implement the requested change.",
        working_directory="/tmp/task-worktree",
        continuation_command='./ww complete TASK-1 --role worker --artifact="<result>"',
        workflow_runtime="auto",
        caller_role="manager",
        next_role="worker",
        selected_agent="codex",
        selected_model="gpt-5",
        selected_reasoning="high",
        requested_profile="developer",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "## Manager: delegate the `develop` assignment" in rendered
    assert (
        "You are the manager. Select the worker and give it the bootstrap "
        "command below:" in rendered
    )
    assert "### Worker bootstrap" in rendered
    assert "- Profile: `developer`" in rendered
    assert "Pass only this command to the selected worker" in rendered
    assert "./ww instruction TASK-1 --role worker" in rendered
    assert "with no task details or commentary" in rendered
    assert "### Working directory" not in rendered
    assert "### Work instruction" not in rendered
    assert "### Worker completion command" not in rendered


def test_orchestrate_worker_status_renders_the_full_assignment() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="in_progress",
        item_id="item-1",
        item_name="develop",
        stage="Workflow step",
        step="develop",
        parent=None,
        item_status="in_progress",
        action_kind="prompt",
        action_text="Implement the requested change.",
        working_directory="/tmp/task-worktree",
        continuation_command='./ww complete TASK-1 --role worker --artifact="<result>"',
        workflow_runtime="auto",
        caller_role="worker",
        next_role="worker",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "## Worker: perform `develop`" in rendered
    assert "### Working directory" in rendered
    assert "### Work instruction" in rendered
    assert "### Worker completion command" in rendered
    assert "### Worker bootstrap" not in rendered


def test_stop_enabled_loop_step_gives_worker_a_deterministic_exit_command() -> None:
    instruction = Instruction(
        task_id="TASK-1",
        workflow="task",
        status="in_progress",
        item_id="review",
        item_name="review",
        stage="Nested workflow step",
        step="review-and-fix/review",
        parent="review-and-fix",
        item_status="in_progress",
        action_kind="prompt",
        action_text="Review the implementation.",
        loop_break_prompt="There are no meaningful findings.",
        loop_break_command=(
            './ww loop TASK-1 --break --role worker --artifact="<result>"'
        ),
        continuation_command=(
            './ww complete TASK-1 --role worker --artifact="<result>"'
        ),
        workflow_runtime="auto",
        caller_role="worker",
        next_role="worker",
    )

    rendered = MarkdownOutputAdapter().render_instruction(instruction)

    assert "Break condition: There are no meaningful findings." in rendered
    assert "./ww loop TASK-1 --break --role worker" in rendered
    assert "Condition met — break the loop:" in rendered
    assert "Condition not met — use the worker completion command below." in rendered
    assert "deterministically exits" not in rendered
