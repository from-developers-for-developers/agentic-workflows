# SPDX-License-Identifier: GPL-3.0-or-later
"""File locks, atomic writes, and lock-file cleanup."""

import errno
import fcntl
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from ww.errors import LockError
from ww.locking import TIMEOUT_VARIABLE, FileLocks, timeout_seconds


def test_lock_path_lives_outside_the_guarded_tree(tmp_path: Path) -> None:
    locks = FileLocks(tmp_path)

    path = locks.lock_path(tmp_path / "tasks" / "TASK-1" / "state.json")

    assert path.parent == tmp_path / ".ww" / "locks"
    assert path.suffix == ".lock"


def test_lock_paths_differ_per_target(tmp_path: Path) -> None:
    locks = FileLocks(tmp_path)

    first = locks.lock_path(tmp_path / "tasks" / "a" / "state.json")
    second = locks.lock_path(tmp_path / "tasks" / "b" / "state.json")

    assert first != second


def test_atomic_write_creates_parents_and_leaves_no_temporary_files(
    tmp_path: Path,
) -> None:
    locks = FileLocks(tmp_path)
    target = tmp_path / "tasks" / "TASK-1" / "state.json"

    locks.atomic_write(target, "first\n")
    locks.atomic_write(target, "second\n")

    assert target.read_text(encoding="utf-8") == "second\n"
    assert [entry.name for entry in target.parent.iterdir()] == ["state.json"]


def test_atomic_write_syncs_the_file_before_and_the_directory_after_the_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Power loss, not only process death, must not lose a committed state."""
    synced: list[str] = []
    real_fsync = os.fsync

    def recording(descriptor: int) -> None:
        kind = "directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file"
        synced.append(kind)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording)
    target = tmp_path / "tasks" / "TASK-1" / "state.json"

    FileLocks(tmp_path).atomic_write(target, "{}\n")

    assert synced == ["file", "directory"]


def test_append_line_terminates_records(tmp_path: Path) -> None:
    locks = FileLocks(tmp_path)
    target = tmp_path / ".ww" / "executions.jsonl"

    locks.append_line(target, "one")
    locks.append_line(target, "two\n")

    assert target.read_text(encoding="utf-8") == "one\ntwo\n"


def test_lock_is_released_when_the_body_raises(tmp_path: Path) -> None:
    locks = FileLocks(tmp_path)
    target = tmp_path / "tasks" / "TASK-1"

    with pytest.raises(RuntimeError), locks.lock(target):
        raise RuntimeError("boom")

    with locks.lock(target):
        pass


def test_cleanup_removes_inactive_sidecars(tmp_path: Path) -> None:
    locks = FileLocks(tmp_path)
    target = tmp_path / "tasks" / "TASK-1"

    with locks.lock(target):
        pass

    assert locks.lock_path(target).exists()
    assert locks.cleanup() == 1
    assert not locks.lock_path(target).exists()


def test_cleanup_waits_for_active_lock_users(tmp_path: Path) -> None:
    locks = FileLocks(tmp_path)
    target = tmp_path / "tasks" / "TASK-1"
    holding = threading.Event()
    release = threading.Event()
    cleaned = threading.Event()

    def hold() -> None:
        with locks.lock(target):
            holding.set()
            release.wait(5)

    def cleanup() -> None:
        locks.cleanup()
        cleaned.set()

    holder = threading.Thread(target=hold)
    cleaner = threading.Thread(target=cleanup)
    holder.start()
    holding.wait(5)
    cleaner.start()
    time.sleep(0.05)
    assert not cleaned.is_set()
    release.set()
    holder.join(5)
    cleaner.join(5)
    assert cleaned.is_set()


def test_lock_opens_sidecar_only_after_activity_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cleanup cannot unlink a sidecar between open and gate acquisition."""
    locks = FileLocks(tmp_path)
    target = tmp_path / "tasks" / "TASK-1"
    sidecar = locks.lock_path(target)
    opened = threading.Event()
    cleaned = threading.Event()
    errors: list[BaseException] = []
    original_open = Path.open

    def hooked_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        handle = original_open(file, *args, **kwargs)
        if file == sidecar and not opened.is_set():
            # A cleanup process needs an exclusive activity lock before it can
            # unlink this sidecar.  Prove that the shared gate is already held
            # at the exact point where the sidecar becomes open.
            with (
                original_open(
                    locks._activity_path, "a+", encoding="utf-8"
                ) as activity_probe,
                pytest.raises(BlockingIOError),
            ):
                fcntl.flock(activity_probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            opened.set()

            def cleanup() -> None:
                try:
                    locks.cleanup()
                except BaseException as error:  # pragma: no cover - assertion below
                    errors.append(error)
                finally:
                    cleaned.set()

            threading.Thread(target=cleanup).start()
        return handle

    monkeypatch.setattr(Path, "open", hooked_open)
    with locks.lock(target):
        assert opened.wait(1)
        time.sleep(0.05)
        assert not cleaned.is_set()
        assert sidecar.exists()
    deadline = time.monotonic() + 1
    while not cleaned.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not errors
    assert cleaned.is_set()


def test_a_second_thread_waits_for_the_holder(tmp_path: Path) -> None:
    locks = FileLocks(tmp_path)
    target = tmp_path / "tasks" / "TASK-1"
    order: list[str] = []
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with locks.lock(target):
            order.append("first-acquired")
            holding.set()
            release.wait(5)
            order.append("first-released")

    def contend() -> None:
        holding.wait(5)
        with locks.lock(target):
            order.append("second-acquired")

    first = threading.Thread(target=hold)
    second = threading.Thread(target=contend)
    first.start()
    second.start()
    holding.wait(5)
    time.sleep(0.05)
    release.set()
    first.join(5)
    second.join(5)

    assert order == ["first-acquired", "first-released", "second-acquired"]


def test_waiting_beyond_the_timeout_raises_lock_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TIMEOUT_VARIABLE, "0.2")
    locks = FileLocks(tmp_path)
    target = tmp_path / "tasks" / "TASK-1"
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with locks.lock(target):
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    holding.wait(5)
    try:
        with pytest.raises(LockError) as error, locks.lock(target):
            pass
    finally:
        release.set()
        holder.join(5)

    assert "timed out" in str(error.value)
    assert TIMEOUT_VARIABLE in str(error.value)


def test_timeout_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TIMEOUT_VARIABLE, raising=False)
    assert timeout_seconds() == 30.0

    monkeypatch.setenv(TIMEOUT_VARIABLE, "5")
    assert timeout_seconds() == 5.0

    monkeypatch.setenv(TIMEOUT_VARIABLE, "0")
    assert timeout_seconds() is None

    monkeypatch.setenv(TIMEOUT_VARIABLE, "soon")
    with pytest.raises(LockError):
        timeout_seconds()


def test_non_contention_os_error_raises_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locks = FileLocks(tmp_path)
    target = tmp_path / "tasks" / "TASK-1"

    def fail_flock(fd: int, operation: int) -> None:
        raise OSError(errno.EBADF, "Bad file descriptor")

    monkeypatch.setattr(fcntl, "flock", fail_flock)
    start = time.monotonic()
    with pytest.raises(LockError) as exc_info, locks.lock(target):
        pass
    elapsed = time.monotonic() - start

    assert elapsed < 0.2
    assert "cannot lock" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.__cause__.errno == errno.EBADF


def test_non_contention_error_raises_with_indefinite_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TIMEOUT_VARIABLE, "0")
    locks = FileLocks(tmp_path)
    target = tmp_path / "tasks" / "TASK-1"

    def fail_flock(fd: int, operation: int) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(fcntl, "flock", fail_flock)
    with pytest.raises(LockError) as exc_info, locks.lock(target):
        pass

    assert "cannot lock" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, OSError)
    assert exc_info.value.__cause__.errno == errno.ENOTSUP


def test_timeout_preserves_contention_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TIMEOUT_VARIABLE, "0.05")
    locks = FileLocks(tmp_path)
    target = tmp_path / "tasks" / "TASK-1"

    def fail_contention(fd: int, operation: int) -> None:
        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(fcntl, "flock", fail_contention)
    with pytest.raises(LockError) as exc_info, locks.lock(target):
        pass

    assert "timed out" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, BlockingIOError)
