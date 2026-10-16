# SPDX-License-Identifier: GPL-3.0-or-later
"""What the page follows: the configuration's files and the task store.

The files come from the core: the YAML levels and the imports they fold,
the JSON settings levels, the rule files the configuration declares and the
rule-automation store. A level that has no file yet is watched too, so
creating ``ww.local.yaml`` is noticed. Polling keeps this dependency-free;
a change is a different modification time or size, or a file appearing or
going away.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from ww.config.composition import configuration_files
from ww.config_files import settings_levels, workflow_levels
from ww.errors import WwError
from ww.project_config import compose_settings
from ww.rule_conversion import every_rule
from ww.rule_store import RuleStore

from .project import Project

Fingerprint = tuple[tuple[str, int, int], ...]
# The runtime directories whose contents the Tasks tab shows: the tasks
# themselves and what extensions record about them, such as a task's branch.
TASK_DIRECTORIES = ("tasks", "ext")


def configuration_paths(project: Project) -> tuple[Path, ...]:
    storage = project.storage
    paths = {level.path for level in workflow_levels(storage.config_path)}
    paths |= {level.path for level in settings_levels(storage.project_config_path)}
    with contextlib.suppress(WwError, OSError, ValueError):
        paths |= set(configuration_files(storage.config_path))
    with contextlib.suppress(WwError, OSError, ValueError):
        paths |= set(compose_settings(storage.project_config_path)[1])
    paths.add(RuleStore(storage.config_root).path)
    if project.configuration is not None:
        for rule in every_rule(project.configuration):
            if rule.source:
                source = Path(rule.source)
                # The directory too: a rule file added to a group's directory.
                paths |= {source, source.parent}
    return tuple(sorted(paths))


def files_fingerprint(paths: Iterable[Path]) -> Fingerprint:
    entries = []
    for path in paths:
        try:
            status = path.stat()
        except OSError:
            entries.append((str(path), -1, -1))
            continue
        entries.append((str(path), status.st_mtime_ns, status.st_size))
    return tuple(entries)


def tree_fingerprint(directories: Iterable[Path]) -> Fingerprint:
    entries = []
    for directory in directories:
        for path in Path(directory).rglob("*"):
            with contextlib.suppress(OSError):
                if path.is_file():
                    status = path.stat()
                    entries.append((str(path), status.st_mtime_ns, status.st_size))
    entries.sort()
    return tuple(entries)


class Watcher:
    """Polls the watched files and counts each kind of change.

    ``config`` and ``tasks`` are versions: each change to the configuration
    or the task store raises its own by one, and wakes every waiter.
    """

    def __init__(
        self,
        load: Callable[[], Project],
        *,
        interval: float = 1.0,
    ) -> None:
        self._load = load
        self.interval = interval
        self.config = 0
        self.tasks = 0
        self.changed = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        project = load()
        self._task_roots = tuple(
            project.storage.runtime_path / name for name in TASK_DIRECTORIES
        )
        self._paths = configuration_paths(project)
        self._config_print = files_fingerprint(self._paths)
        self._tasks_print = tree_fingerprint(self._task_roots)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self.changed:
            self.changed.notify_all()
        if self._thread is not None:
            self._thread.join()

    def poll(self) -> None:
        """Look once; the thread does this every ``interval`` seconds."""
        config = files_fingerprint(self._paths)
        tasks = tree_fingerprint(self._task_roots)
        config_changed = config != self._config_print
        tasks_changed = tasks != self._tasks_print
        if config_changed:
            # An edit may add an import or a rule file: look again at what to watch.
            self._paths = configuration_paths(self._load())
            config = files_fingerprint(self._paths)
        self._config_print, self._tasks_print = config, tasks
        if not (config_changed or tasks_changed):
            return
        with self.changed:
            self.config += int(config_changed)
            self.tasks += int(tasks_changed)
            self.changed.notify_all()

    def wait(self, seen: tuple[int, int], timeout: float) -> tuple[int, int]:
        """Block until the versions differ from ``seen`` or ``timeout`` passes."""
        with self.changed:
            self.changed.wait_for(
                lambda: (self.config, self.tasks) != seen or self._stop.is_set(),
                timeout,
            )
            return self.config, self.tasks

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            with contextlib.suppress(Exception):
                self.poll()
