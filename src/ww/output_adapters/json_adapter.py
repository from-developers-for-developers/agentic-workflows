# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON output adapter."""

from __future__ import annotations

import json

from ww.instructions import Instruction
from ww.output_adapters.base import OutputAdapter
from ww.results import InitializationResult, ResetResult


class JsonOutputAdapter(OutputAdapter):
    """Render stable JSON for programmatic consumers."""

    def render_instruction(self, instruction: Instruction) -> str:
        return json.dumps(instruction.to_dict(), indent=2)

    def render_reset(self, result: ResetResult) -> str:
        return json.dumps(
            {"task_id": result.task_id, "removed": result.removed}, indent=2
        )

    def render_initialization(self, result: InitializationResult) -> str:
        return json.dumps(
            {
                "root": result.root,
                "workflow_config": "ww-agentic-workflows.yaml",
                "project_config": "ww-agentic-workflows.json",
                "launcher": "ww",
                "created": list(result.created),
                "preserved": list(result.preserved),
                "manual_actions": list(result.actions),
            },
            indent=2,
        )
