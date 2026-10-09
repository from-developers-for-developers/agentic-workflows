# SPDX-License-Identifier: GPL-3.0-or-later
"""Exclusive file locking and atomic replacement for ww's writes.

Two ww invocations in one project are unrelated processes competing for the
same task files. Atomic replacement alone is not enough: it stops a reader
seeing half a write, but not two processes reading one task's state, each
deciding the next item, and one overwriting the other. The fix is a single
exclusive lock per task, held by ``WorkflowService`` for the whole of ``start``,
``next``, ``complete`` or ``reset`` — the span, not the individual write.

The task scope is the primary coordination boundary; the project scope wraps
``ww init``, project metadata has a dedicated merge lock, the execution log has
its own lock, and aggregate commits use a separate per-aggregate CAS lock for
direct storage-adapter callers. Everything else writes inside one of those scopes, so
``atomic_write`` takes no lock of its own. A shared activity gate wraps those
locks; maintenance takes it exclusively before pruning sidecars.

Reads are deliberately unlocked. Replacement is atomic, so a reader always sees
complete files. A task is a task index plus one document per run, and the index
names the revision each run document must carry; a reader that meets run
documents newer than the index it read waits for the aggregate lock and reads
again. ``RunCoordinator.load`` selects the requested run, execution state, and
plan snapshot from one decoded revision. Read-only commands therefore wait only
in the rare moment a writer is between its run documents and its index.

Locks are advisory POSIX locks on sidecar files under ``.ww/locks/``. The
kernel releases them when a process exits, so a killed run leaves nothing
stale. Waiting order is unspecified — no operating system promises FIFO — and
every wait is bounded by ``WW_LOCK_TIMEOUT`` (seconds, default 30; ``0`` waits
indefinitely) so a pathological wait fails loudly instead of hanging.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from ww.errors import LockError

TIMEOUT_VARIABLE = "WW_LOCK_TIMEOUT"
DEFAULT_TIMEOUT_SECONDS = 30.0
_NOTICE_AFTER_SECONDS = 0.25
_MAXIMUM_POLL_SECONDS = 0.05


class FileLocks:
    """Exclusive locks and atomic writes for one project root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.directory = self.root / ".ww" / "locks"

    def lock_path(self, target: Path) -> Path:
        """Return the sidecar lock file that guards ``target``.

        The lock never sits next to the file it guards: atomic replacement
        swaps the target's inode and `reset` deletes whole task directories, so
        a sidecar inside the task tree would be destroyed by the very writes it
        is meant to protect.
        """
        absolute = os.path.normpath(Path(target).absolute())
        digest = hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:32]
        return self.directory / f"{digest}.lock"

    @property
    def _activity_path(self) -> Path:
        return self.directory / ".activity.lock"

    @contextmanager
    def lock(self, target: Path, *, purpose: str | None = None) -> Iterator[None]:
        """Hold an exclusive lock on ``target`` for the duration of the block.

        Not reentrant: ww takes each lock at exactly one place.
        """
        path = self.lock_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Closing a handle releases its lock, including when the block raised.
        with self._activity_path.open("a+", encoding="utf-8") as activity:
            # Cleanup takes the activity gate exclusively before unlinking
            # sidecars.  Do not open the task sidecar until the shared gate is
            # held: otherwise cleanup can unlink it between open(2) and the
            # shared-lock acquisition, leaving this process locking an orphan
            # inode while a later caller locks the replacement path.
            _acquire(activity, "ww activity gate", fcntl.LOCK_SH)
            with path.open("a+", encoding="utf-8") as handle:
                _acquire(handle, purpose or self._describe(target))
                yield

    def cleanup(self) -> int:
        """Remove unused lock sidecars without racing active or waiting users."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with self._activity_path.open("a+", encoding="utf-8") as activity:
            _acquire(activity, "ww activity gate", fcntl.LOCK_EX)
            removed = 0
            for path in self.directory.glob("*.lock"):
                if path == self._activity_path:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
            return removed

    def atomic_write(self, target: Path, content: str) -> None:
        """Replace ``target`` with ``content`` in one step.

        This takes no lock. Callers write inside a scope their command already
        holds; the atomicity here is what keeps a concurrent *reader* from ever
        seeing a partial file.
        """
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
            directory_descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def append_line(self, target: Path, line: str) -> None:
        """Append one newline-terminated record under ``target``'s own lock."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.lock(target), target.open("a", encoding="utf-8") as handle:
            handle.write(line if line.endswith("\n") else line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _describe(self, target: Path) -> str:
        try:
            return str(Path(target).relative_to(self.root))
        except ValueError:
            return str(target)


def timeout_seconds() -> float | None:
    """Return the configured wait bound, or ``None`` to wait indefinitely."""
    raw = os.environ.get(TIMEOUT_VARIABLE)
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as error:
        raise LockError(
            f"invalid {TIMEOUT_VARIABLE}: {raw!r} is not a number of seconds"
        ) from error
    return None if value <= 0 else value


_CONTENDED_ERRNOS = {
    errno.EAGAIN,
    errno.EWOULDBLOCK,
    errno.EACCES,
    errno.EINTR,
}


def _is_contended_error(error: OSError) -> bool:
    return isinstance(error, BlockingIOError) or error.errno in _CONTENDED_ERRNOS


def _acquire(handle: TextIO, description: str, mode: int = fcntl.LOCK_EX) -> None:
    limit = timeout_seconds()
    started = time.monotonic()
    deadline = None if limit is None else started + limit
    announced = False
    delay = 0.001
    last_contention_error: OSError | None = None
    while True:
        try:
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
            return
        except OSError as error:
            if not _is_contended_error(error):
                raise LockError(f"cannot lock {description}: {error}") from error
            last_contention_error = error
        if deadline is not None and time.monotonic() >= deadline:
            raise LockError(
                f"timed out after {limit:g}s waiting for another ww process to "
                f"release {description}; set {TIMEOUT_VARIABLE} to wait longer "
                "(or 0 to wait indefinitely)"
            ) from last_contention_error
        if not announced and time.monotonic() - started >= _NOTICE_AFTER_SECONDS:
            sys.stderr.write(
                f"ww: waiting for another ww process to release {description}\n"
            )
            sys.stderr.flush()
            announced = True
        time.sleep(delay)
        delay = min(delay * 2, _MAXIMUM_POLL_SECONDS)
