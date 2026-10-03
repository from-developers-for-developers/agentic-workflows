# SPDX-License-Identifier: GPL-3.0-or-later
"""Persisted, plan-driven workflow execution models.

These models deliberately contain no YAML or handler-resolution concepts.  A
task starts with a compiled :class:`WorkflowPlan` snapshot and progresses only
through the normalized plan items in that snapshot.
"""

from __future__ import annotations

from typing import Any, cast

from ww.actions import Commands, actions
from ww.contracts import (
    CheckSource,
    ChildOperation,
    ExecutionKind,
    ItemAssignment,
    ItemOperation,
    LoopAssignment,
    PlanItemOwner,
    PlanItemPhase,
    StepRole,
)
from ww.operations import decode_operation
from ww.plan import (
    PlanItem,
    PlannedCheck,
    PlannedMode,
    PlannedRule,
    VerificationTarget,
    WorkflowPlan,
)
from ww.validation import (
    expect_bool,
    expect_literal,
    expect_optional_string,
    expect_positive_int,
    expect_string,
    is_positive_int,
    require_keys,
)
from ww.workflow_config import (
    ChoiceDefinition,
    DocumentDefinition,
    DocumentScope,
    DocumentUpdate,
    ItemFieldUpdate,
    MetadataScope,
    ProvidedVariable,
    RuleHints,
    SavedMetadata,
)
from ww.workspace import Workdir


def _plan_from_dict(data: Any) -> WorkflowPlan:
    if not isinstance(data, dict):
        raise ValueError("plan must be a mapping")
    required = {
        "workflow",
        "workflow_description",
        "agent",
        "task_id",
        "modes",
        "handoff",
        "items",
    }
    require_keys(data, required, "plan")
    if not isinstance(data["items"], list) or not isinstance(data["modes"], list):
        raise ValueError("plan items and modes must be lists")
    items = tuple(
        _plan_item_from_dict(raw, item_index, data["agent"])
        for item_index, raw in enumerate(data["items"])
    )
    if not all(isinstance(item, str) for item in data["modes"]):
        raise ValueError("plan modes must be strings")
    if not isinstance(data["handoff"], bool):
        raise ValueError("plan handoff must be a boolean")
    return WorkflowPlan(
        workflow=expect_string(data["workflow"], "workflow"),
        workflow_description=expect_string(
            data["workflow_description"], "workflow description"
        ),
        agent=expect_string(data["agent"], "agent"),
        task_id=expect_optional_string(data["task_id"], "task ID"),
        modes=tuple(data["modes"]),
        handoff=data["handoff"],
        items=items,
        documents=_documents_from_list(data.get("documents", [])),
        recommended_next_workflow=expect_optional_string(
            data.get("recommended_next_workflow"), "recommended next workflow"
        ),
        hooks_from=expect_optional_string(data.get("hooks_from"), "hooks_from"),
    )


def _plan_item_from_dict(raw: Any, item_index: int, default_agent: Any) -> PlanItem:
    if not isinstance(raw, dict):
        raise ValueError(f"plan.items[{item_index}] must be a mapping")
    item_path = f"plan.items[{item_index}]"
    provide = _provided_variables_from_list(raw.get("provide", []), item_path)
    save_metadata = _saved_metadata_from_list(raw.get("save_metadata", []), item_path)
    update_document = _document_updates_from_list(
        raw.get("update_document", []), item_path
    )
    outputs = _string_list(raw.get("outputs", []), "plan item outputs")
    ancestors = _string_list(raw.get("ancestors", []), "plan item ancestors")
    step_ordinals = _positive_int_list(
        raw.get("step_ordinals", []), "plan item step_ordinals"
    )
    dependencies = _string_list(raw.get("dependencies", []), "plan item dependencies")
    return PlanItem(
        id=expect_string(raw.get("id"), "plan item ID"),
        position=expect_positive_int(raw.get("position"), "plan position"),
        name=expect_string(raw.get("name"), "plan item name"),
        description=expect_string(raw.get("description", ""), "description"),
        on_failure=expect_string(raw.get("on_failure", "operator"), "on_failure"),
        on_failure_instruction=expect_optional_string(
            raw.get("on_failure_instruction"), "on_failure_instruction"
        ),
        max_handler_fixes=expect_positive_int(
            raw.get("max_handler_fixes", 3), "max_handler_fixes"
        ),
        operation=decode_operation(raw.get("operation")),
        owner=_plan_item_owner(raw.get("owner")),
        execution=_execution_kind(raw.get("execution")),
        requires_agent_input=expect_bool(
            raw.get("requires_agent_input"), f"{item_path}.requires_agent_input"
        ),
        workflow=expect_string(raw.get("workflow"), "workflow"),
        step=expect_string(raw.get("step"), "step"),
        parent=expect_optional_string(raw.get("parent"), "parent"),
        phase=_plan_item_phase(raw.get("phase")),
        source=expect_string(raw.get("source"), "source"),
        registered_handler=expect_optional_string(
            raw.get("registered_handler"), "registered handler"
        ),
        provide=provide,
        save_metadata=save_metadata,
        update_document=update_document,
        update_item=_item_field_updates_from_list(
            raw.get("update_item", []), item_path
        ),
        item_identity=expect_optional_string(
            raw.get("item_identity"), f"{item_path}.item_identity"
        ),
        item_unique=_string_list(
            raw.get("item_unique", []), f"{item_path}.item_unique"
        ),
        outputs=outputs,
        dependencies=dependencies,
        requested_agent=expect_optional_string(
            raw.get("requested_agent", default_agent), "requested agent"
        ),
        requested_model=expect_optional_string(
            raw.get("requested_model", raw.get("model", "auto")), "requested model"
        ),
        requested_reasoning=expect_optional_string(
            raw.get("requested_reasoning", raw.get("reasoning", "auto")),
            "requested reasoning",
        ),
        role=cast(
            StepRole,
            expect_literal(raw.get("role", "worker"), StepRole, f"{item_path}.role"),
        ),
        subagents=expect_bool(raw.get("subagents", True), f"{item_path}.subagents"),
        interactive=expect_bool(
            raw.get("interactive", False), f"{item_path}.interactive"
        ),
        choices=_choices_from_list(raw.get("choices", []), item_path),
        ui=expect_bool(raw.get("ui", False), f"{item_path}.ui"),
        model=expect_optional_string(raw.get("model"), "model"),
        reasoning=expect_optional_string(raw.get("reasoning"), "reasoning"),
        profile=expect_optional_string(raw.get("profile"), "profile"),
        profile_instruction=expect_optional_string(
            raw.get("profile_instruction"), "profile instruction"
        ),
        profile_path=expect_optional_string(raw.get("profile_path"), "profile path"),
        workdir=_workdir(raw.get("workdir", "task")),
        summary=expect_bool(raw.get("summary", False), f"{item_path}.summary"),
        item_operation=_item_operation(raw.get("item_operation")),
        item_template=expect_bool(
            raw.get("item_template", False), f"{item_path}.item_template"
        ),
        item_id=expect_optional_string(raw.get("item_id"), "item ID"),
        item_assignment=_item_assignment(raw.get("item_assignment", "per_step")),
        loop_id=expect_optional_string(raw.get("loop_id"), "loop ID"),
        loop_assignment=_loop_assignment(raw.get("loop_assignment")),
        shared_items=expect_bool(
            raw.get("shared_items", False), f"{item_path}.shared_items"
        ),
        split_instruction=expect_optional_string(
            raw.get("split_instruction"), "split instruction"
        ),
        artifact=expect_bool(raw.get("artifact", True), f"{item_path}.artifact"),
        child_operation=_child_operation(raw.get("child_operation")),
        child_identity=expect_bool(
            raw.get("child_identity", False), f"{item_path}.child_identity"
        ),
        child_stage=expect_optional_string(
            raw.get("child_stage"), f"{item_path}.child_stage"
        ),
        child_number=_optional_positive_int(
            raw.get("child_number"), f"{item_path}.child_number"
        ),
        ancestors=ancestors,
        step_ordinals=step_ordinals,
        artifact_dependency=expect_optional_string(
            raw.get("artifact_dependency"), "artifact dependency"
        ),
        loop_break=expect_optional_string(raw.get("loop_break"), "loop break"),
        loop_continue=expect_optional_string(raw.get("loop_continue"), "loop continue"),
        assessment_question=expect_optional_string(
            raw.get("assessment_question"), "assessment question"
        ),
        assessment_outcomes=tuple(
            _string_list(raw.get("assessment_outcomes", []), "assessment outcomes")
        ),
        assessment_stops=tuple(
            _string_list(raw.get("assessment_stops", []), "assessment stops")
        ),
        assessment_parent=expect_optional_string(
            raw.get("assessment_parent"), "assessment parent"
        ),
        assessment_outcome=expect_optional_string(
            raw.get("assessment_outcome"), "assessment outcome"
        ),
        rules=_planned_rules_from_list(raw.get("rules", []), item_path),
        checks=_planned_checks_from_list(raw.get("checks", []), item_path),
        modes=_planned_modes_from_list(raw.get("modes", []), item_path),
        verifies=_verification_target(raw.get("verifies"), item_path),
    )


def _verification_target(value: Any, item_path: str) -> VerificationTarget | None:
    if value is None:
        return None
    path = f"{item_path}.verifies"
    if not isinstance(value, dict) or not set(value) <= {
        "item_id",
        "ordinal",
        "hints",
    }:
        raise ValueError(f"{path} must be an object of item_id, ordinal, hints")
    require_keys(value, {"item_id", "ordinal"}, path)
    return VerificationTarget(
        item_id=expect_string(value["item_id"], f"{path}.item_id"),
        ordinal=expect_positive_int(value["ordinal"], f"{path}.ordinal"),
        hints=_rule_hints(value.get("hints", {}), f"{path}.hints"),
    )


def _planned_modes_from_list(value: Any, item_path: str) -> tuple[PlannedMode, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{item_path}.modes must be a list")
    return tuple(
        PlannedMode.from_dict(raw, f"{item_path}.modes[{index}]")
        for index, raw in enumerate(value)
    )


def _planned_rules_from_list(value: Any, item_path: str) -> tuple[PlannedRule, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{item_path}.rules must be a list")
    result = []
    for index, raw in enumerate(value):
        path = f"{item_path}.rules[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must be an object")
        require_keys(
            raw,
            {"id", "summary", "text", "text_hash", "paths", "has_command", "max_fixes"},
            path,
        )
        result.append(
            PlannedRule(
                id=expect_string(raw["id"], f"{path}.id"),
                summary=expect_string(raw["summary"], f"{path}.summary"),
                text=expect_string(raw["text"], f"{path}.text"),
                text_hash=expect_string(raw["text_hash"], f"{path}.text_hash"),
                paths=_string_list(raw["paths"], f"{path}.paths"),
                has_command=expect_bool(raw["has_command"], f"{path}.has_command"),
                max_fixes=expect_positive_int(raw["max_fixes"], f"{path}.max_fixes"),
                hints=_rule_hints(raw.get("hints", {}), f"{path}.hints"),
                source=expect_optional_string(raw.get("source"), f"{path}.source"),
            )
        )
    return tuple(result)


def _rule_hints(value: Any, path: str) -> RuleHints:
    if not isinstance(value, dict) or not set(value) <= {
        "agent",
        "model",
        "reasoning",
    }:
        raise ValueError(f"{path} must be an object of agent, model, reasoning")
    return RuleHints(
        expect_optional_string(value.get("agent"), f"{path}.agent"),
        expect_optional_string(value.get("model"), f"{path}.model"),
        expect_optional_string(value.get("reasoning"), f"{path}.reasoning"),
    )


def _planned_checks_from_list(value: Any, item_path: str) -> tuple[PlannedCheck, ...]:
    """Decode planned checks; resolved derived checks on records use it too."""
    if not isinstance(value, list):
        raise ValueError(f"{item_path}.checks must be a list")
    result = []
    for index, raw in enumerate(value):
        path = f"{item_path}.checks[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{path} must be an object")
        require_keys(
            raw, {"id", "source", "summary", "command", "paths", "max_fixes"}, path
        )
        command = raw["command"]
        if not isinstance(command, dict):
            raise ValueError(f"{path}.command must be an object")
        decoded = actions.get("cli").decode(command)
        if not isinstance(decoded, Commands):  # pragma: no cover - cli contract
            raise ValueError(f"{path}.command is not a command")
        result.append(
            PlannedCheck(
                id=expect_string(raw["id"], f"{path}.id"),
                source=cast(
                    CheckSource,
                    expect_literal(raw["source"], CheckSource, f"{path}.source"),
                ),
                summary=expect_string(raw["summary"], f"{path}.summary"),
                command=decoded,
                paths=_string_list(raw["paths"], f"{path}.paths"),
                on_failure_instruction=expect_optional_string(
                    raw.get("on_failure_instruction"), "check failure instruction"
                ),
                max_fixes=expect_positive_int(raw["max_fixes"], f"{path}.max_fixes"),
                covers=_string_list(raw.get("covers", []), f"{path}.covers"),
            )
        )
    return tuple(result)


def _provided_variables_from_list(
    value: Any, item_path: str
) -> tuple[ProvidedVariable, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{item_path}.provide must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{item_path}.provide entries must be objects")
    return tuple(
        ProvidedVariable(
            expect_string(item.get("name"), "provided name"),
            expect_optional_string(item.get("description", ""), "provided description")
            or "",
        )
        for item in value
    )


def _choices_from_list(value: Any, item_path: str) -> tuple[ChoiceDefinition, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{item_path}.choices must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{item_path}.choices entries must be objects")
    return tuple(
        ChoiceDefinition(
            expect_string(item.get("label"), "choice label"),
            expect_optional_string(item.get("description", ""), "choice description")
            or "",
        )
        for item in value
    )


def _document_updates_from_list(
    value: Any, item_path: str
) -> tuple[DocumentUpdate, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{item_path}.update_document must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{item_path}.update_document entries must be objects")
    return tuple(
        DocumentUpdate(
            expect_string(item.get("name"), "document update name"),
            expect_optional_string(item.get("instruction", ""), "document instruction")
            or "",
        )
        for item in value
    )


def _documents_from_list(value: Any) -> tuple[DocumentDefinition, ...]:
    if not isinstance(value, list):
        raise ValueError("plan.documents must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError("plan.documents entries must be objects")
    return tuple(
        DocumentDefinition(
            expect_string(item.get("name"), "document name"),
            expect_optional_string(item.get("description", ""), "document description")
            or "",
            cast(
                DocumentScope,
                expect_literal(
                    item.get("scope", "task"), DocumentScope, "document scope"
                ),
            ),
            path=expect_optional_string(item.get("path"), "document path"),
        )
        for item in value
    )


def _saved_metadata_from_list(value: Any, item_path: str) -> tuple[SavedMetadata, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{item_path}.save_metadata must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{item_path}.save_metadata entries must be objects")
    return tuple(
        SavedMetadata(
            expect_string(item.get("name"), "saved metadata name"),
            expect_string(item.get("key"), "saved metadata key"),
            expect_optional_string(
                item.get("description", ""), "saved metadata description"
            )
            or "",
            _metadata_scope(item.get("scope", "task")),
            append=expect_bool(item.get("append", False), "saved metadata append"),
        )
        for item in value
    )


def _item_field_updates_from_list(
    value: Any, item_path: str
) -> tuple[ItemFieldUpdate, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{item_path}.update_item must be a list")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{item_path}.update_item entries must be objects")
    return tuple(
        ItemFieldUpdate(
            expect_string(item.get("name"), "item field name"),
            expect_optional_string(item.get("description", ""), "item field text")
            or "",
        )
        for item in value
    )


def _optional_positive_int(value: Any, context: str) -> int | None:
    if value is None:
        return None
    if not is_positive_int(value):
        raise ValueError(f"{context} must be a positive integer or null")
    return int(value)


def _string_list(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{context} must be a list of strings")
    return tuple(value)


def _positive_int_list(value: Any, context: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not all(is_positive_int(item) for item in value):
        raise ValueError(f"{context} must be positive integers")
    return tuple(value)


def _plan_item_owner(value: Any) -> PlanItemOwner:
    return cast(PlanItemOwner, expect_literal(value, PlanItemOwner, "plan item owner"))


def _execution_kind(value: Any) -> ExecutionKind:
    return cast(
        ExecutionKind, expect_literal(value, ExecutionKind, "plan item execution")
    )


def _metadata_scope(value: Any) -> MetadataScope:
    return cast(
        MetadataScope, expect_literal(value, MetadataScope, "saved metadata scope")
    )


def _plan_item_phase(value: Any) -> PlanItemPhase:
    return cast(PlanItemPhase, expect_literal(value, PlanItemPhase, "plan item phase"))


def _loop_assignment(value: Any) -> LoopAssignment | None:
    if value is None:
        return None
    return cast(
        LoopAssignment, expect_literal(value, LoopAssignment, "loop assignment")
    )


def _item_assignment(value: Any) -> ItemAssignment:
    return cast(
        ItemAssignment, expect_literal(value, ItemAssignment, "item assignment")
    )


def _workdir(value: Any) -> Workdir:
    return cast(Workdir, expect_literal(value, Workdir, "workdir"))


def _item_operation(value: Any) -> ItemOperation | None:
    if value is None:
        return None
    return cast(ItemOperation, expect_literal(value, ItemOperation, "item operation"))


def _child_operation(value: Any) -> ChildOperation | None:
    if value is None:
        return None
    return cast(
        ChildOperation, expect_literal(value, ChildOperation, "child operation")
    )
