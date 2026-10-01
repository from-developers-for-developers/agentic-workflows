# SPDX-License-Identifier: GPL-3.0-or-later
"""Rendering a step's artifact as Markdown."""

from ww.artifacts import render_step_artifact


def test_render_step_artifact_uses_builtin_markdown_structure() -> None:
    assert render_step_artifact(
        task_id="TASK-1",
        workflow="task",
        step="develop",
        step_number=2,
        step_total=3,
        skill="auto",
        result="# finished\n\n",
    ) == (
        "# TASK-1 — develop\n\n"
        "## Workflow context\n\n"
        "- Workflow: task\n"
        "- Step: 2 of 3\n"
        "- Skill: auto\n\n"
        "## Result\n\n"
        "# finished\n"
    )


def test_render_step_artifact_restores_escaped_markdown_line_endings() -> None:
    artifact = render_step_artifact(
        task_id="TASK-1",
        workflow="task",
        step="develop",
        step_number=1,
        step_total=1,
        skill="auto",
        result="## Scope\\n\\nReview the change.\\r\\n\\r\\n## Decision\\n\\nProceed.",
    )

    assert artifact.endswith(
        "## Result\n\n"
        "## Scope\n\n"
        "Review the change.\n\n"
        "## Decision\n\n"
        "Proceed.\n"
    )


def test_render_step_artifact_preserves_literal_escapes_in_multiline_markdown() -> None:
    artifact = render_step_artifact(
        task_id="TASK-1",
        workflow="task",
        step="develop",
        step_number=1,
        step_total=1,
        skill="auto",
        result="## Finding\n\nThe input contained a literal `\\n` sequence.",
    )

    assert artifact.endswith(
        "## Result\n\n"
        "## Finding\n\n"
        "The input contained a literal `\\n` sequence.\n"
    )
