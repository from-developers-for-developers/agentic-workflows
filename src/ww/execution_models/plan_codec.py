# SPDX-License-Identifier: GPL-3.0-or-later
"""Persisted, plan-driven workflow execution models.

These models deliberately contain no YAML or handler-resolution concepts.  A
task starts with a compiled :class:`WorkflowPlan` snapshot and progresses only
through the normalized plan items in that snapshot.
"""

from __future__ import annotations

from typing import Any, cast

from ww.contracts import (
    ChildOperation,
    ExecutionKind,
    ItemAssignment,
    ItemOperation,
    LoopAssignment,
    PlanItemOwner,
    PlanItemPhase,
)
from ww.operations import decode_operation
from ww.plan import PlanItem, WorkflowPlan
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
    DocumentUpdate,
    ItemFieldUpdate,
    MetadataScope,
    ProvidedVariable,
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
    )


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
            _metadata_scope(item.get("scope", "task")),
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
