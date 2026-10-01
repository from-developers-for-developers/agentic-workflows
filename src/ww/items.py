# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable, run-local work items used by item-aware workflow steps."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields, replace

# An item field name, e.g. "acceptance_criteria" or "due-date"; "2nd" does not
# match.
FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


@dataclass(frozen=True)
class WorkItem:
    """One collected unit of work inside a run."""

    id: str
    item: str
    processed_item: str = ""
    proposed_solution: str = ""
    actual_solution: str = ""
    resolved: bool = False
    reported: bool = False
    reference_to_id: str | None = None
    # Custom fields a workflow declares for its items, such as the ID of the
    # source comment or of the reply posted for it; string values only.
    fields: tuple[tuple[str, str], ...] = ()

    def field(self, name: str) -> str | None:
        return dict(self.fields).get(name)

    def with_fields(self, values: dict[str, str]) -> WorkItem:
        merged = {**dict(self.fields), **values}
        return replace(self, fields=tuple(merged.items()))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "item": self.item,
            "processed_item": self.processed_item,
            "proposed_solution": self.proposed_solution,
            "actual_solution": self.actual_solution,
            "resolved": self.resolved,
            "reported": self.reported,
            "reference_to_id": self.reference_to_id,
            "fields": dict(self.fields),
        }

    @classmethod
    def from_dict(cls, data: object) -> WorkItem:
        if not isinstance(data, dict):
            raise ValueError("item must be a mapping")

        if not isinstance(data.get("id"), str) or not data["id"].strip():
            raise ValueError("item ID must be a non-empty string")
        if not isinstance(data.get("item"), str) or not data["item"].strip():
            raise ValueError("item text must be a non-empty string")
        processed = data.get("processed_item", "")
        proposed = data.get("proposed_solution", "")
        actual = data.get("actual_solution", "")
        if not all(isinstance(value, str) for value in (processed, proposed, actual)):
            raise ValueError("item text fields must be strings")
        resolved = data.get("resolved", False)
        reported = data.get("reported", False)
        if not isinstance(resolved, bool) or not isinstance(reported, bool):
            raise ValueError("item resolved and reported fields must be booleans")
        reference = data.get("reference_to_id")
        if reference is not None and (not isinstance(reference, str) or not reference):
            raise ValueError("item reference_to_id must be a non-empty string or null")
        return cls(
            id=data["id"],
            item=data["item"],
            processed_item=processed,
            proposed_solution=proposed,
            actual_solution=actual,
            resolved=resolved,
            reported=reported,
            reference_to_id=reference,
            fields=validate_item_fields(data.get("fields", {})),
        )


def validate_item_fields(data: object) -> tuple[tuple[str, str], ...]:
    """Custom fields are a mapping of valid names to strings."""
    if not isinstance(data, dict):
        raise ValueError("item fields must be a mapping")
    for name, value in data.items():
        if not isinstance(name, str) or not FIELD_NAME.fullmatch(name):
            raise ValueError(f"invalid item field name: {name!r}")
        if not isinstance(value, str):
            raise ValueError(f"item field {name!r} must be a string")
    return tuple(data.items())


# Fields an agent may update after collection; identity and the item text are fixed.
EDITABLE_WORK_ITEM_FIELDS = frozenset(
    field.name for field in fields(WorkItem) if field.name not in {"id", "item"}
)
