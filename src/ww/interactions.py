# SPDX-License-Identifier: GPL-3.0-or-later
"""The per-task record of conversations held with the operator.

An interactive step is a conversation the agent holds with the operator in
its own session; ww cannot hear it.  When the conversation ends, the agent
records both sides at once with ``interact --transcript``, and ww appends
each entry to one file per task, ``interactions.md``, never rewriting it.
Every entry names the run and step it belongs to, so the file reads as the
task's whole history of operator involvement and the conversation of one step
can be read back out of it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ww.errors import StateError
from ww.storage import Storage

INTERACTIONS_FILE = "interactions.md"
_SEPARATOR = " · "
# A heading has time, run, step, and speaker.
_FIELDS = 4
# A transcript line that starts an entry: a speaker marker in any case, plain
# or bold with the colon inside or outside the bold, then the entry's first
# line, e.g. "**Operator:** Use a queue." or "agent: I propose a queue.".
_MARKER = re.compile(
    r"^\s*(?:\*\*)?(Agent|Operator)(?:\*\*)?:(?:\*\*)?\s*(.*)$", re.IGNORECASE
)
_TRANSCRIPT_FORMAT = (
    "a transcript is lines starting with `Agent:` or `Operator:`, each "
    "followed by that side's words; the lines until the next marker belong "
    "to the same entry"
)


@dataclass(frozen=True)
class InteractionEntry:
    """One recorded entry: who said what, in which run and step."""

    at: str
    run_id: str | None
    step: str
    speaker: str
    text: str

    def to_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "run_id": self.run_id,
            "step": self.step,
            "speaker": self.speaker,
            "text": self.text,
        }


class InteractionLog:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def path(self, task_id: str) -> Path:
        return self.storage.runtime_path / "tasks" / task_id / INTERACTIONS_FILE

    def append(
        self,
        task_id: str,
        *,
        run_id: str | None,
        step: str,
        speaker: str,
        text: str,
        at: str,
    ) -> None:
        """Append one entry; the file is created with a title on first use."""
        self.append_entries(
            task_id,
            ((speaker, text),),
            run_id=run_id,
            step=step,
            at=at,
        )

    def append_entries(
        self,
        task_id: str,
        spoken: Sequence[tuple[str, str]],
        *,
        run_id: str | None,
        step: str,
        at: str,
    ) -> None:
        """Append ``(speaker, text)`` entries in order, in one write."""
        if not spoken:
            return
        path = self.path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = [at, run_id or "-", step]
        chunks = []
        for speaker, text in spoken:
            chunks.append(f"## {_SEPARATOR.join([*fields, speaker])}\n\n")
            if text.strip():
                chunks.append(text.strip() + "\n\n")
        with path.open("a", encoding="utf-8") as handle:
            if handle.tell() == 0:
                handle.write(f"# {task_id} — interactions with the operator\n\n")
            handle.write("".join(chunks))

    def read(self, task_id: str) -> str:
        path = self.path(task_id)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def entries(self, task_id: str) -> tuple[InteractionEntry, ...]:
        """Read the record back as entries, in the order they were appended."""
        result: list[InteractionEntry] = []
        heading: InteractionEntry | None = None
        body: list[str] = []
        for line in self.read(task_id).splitlines():
            parsed = _parse_heading(line)
            if parsed is None:
                body.append(line)
                continue
            if heading is not None:
                result.append(replace(heading, text="\n".join(body).strip()))
            heading, body = parsed, []
        if heading is not None:
            result.append(replace(heading, text="\n".join(body).strip()))
        return tuple(result)

    def remove(self, task_id: str) -> None:
        """Forget a task's record, as part of resetting the task."""
        self.path(task_id).unlink(missing_ok=True)


def parse_transcript(text: str) -> tuple[tuple[str, str], ...]:
    """A plain transcript as ordered ``(speaker, text)`` entries.

    Speakers are ``operator`` and ``agent``; an entry with no words is
    dropped.  Text before the first marker, or no entry at all, is refused.
    """
    entries: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        match = _MARKER.match(line)
        if match is not None:
            entries.append((match.group(1).lower(), [match.group(2)]))
        elif entries:
            entries[-1][1].append(line)
        elif line.strip():
            raise StateError(
                f"the transcript starts with {line.strip()[:40]!r}; "
                + _TRANSCRIPT_FORMAT
            )
    result = tuple(
        (speaker, "\n".join(lines).strip())
        for speaker, lines in entries
        if "\n".join(lines).strip()
    )
    if not result:
        raise StateError("the transcript has no entries; " + _TRANSCRIPT_FORMAT)
    return result


def _parse_heading(line: str) -> InteractionEntry | None:
    """An entry heading: time, run, step, and the speaker."""
    if not line.startswith("## "):
        return None
    fields = line[3:].split(_SEPARATOR)
    if len(fields) != _FIELDS:
        return None
    at, run_id, step = fields[:3]
    return InteractionEntry(at, None if run_id == "-" else run_id, step, fields[-1], "")
