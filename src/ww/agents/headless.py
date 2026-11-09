# SPDX-License-Identifier: GPL-3.0-or-later
"""Running an agent's command-line program headless: what every adapter shares.

A :class:`HeadlessLauncher` subclass describes one agent's program: its
default name, how it reports its version and login, which models and
reasoning levels it takes, and the command that starts or resumes it.
This module turns that description into checks — whether the program is
there, recent enough, logged in, and accepts the step's model and
reasoning — and into the command to run.

Checks return :class:`Finding` s and never print: an error means the agent
must not be started, a warning that something could not be confirmed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from abc import abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from .launch import (
    HeadlessSettings,
    Launcher,
    LaunchRequest,
    Permission,
    ResultRecording,
)
from .usage import TokenCounts

Level = Literal["error", "warning"]
# How long ww waits for a program's version, login or model list.
PROBE_SECONDS = 20.0
Version = tuple[int, ...]


@dataclass(frozen=True)
class Finding:
    """One thing a check found; ``subject`` says which part it is about."""

    level: Level
    subject: str
    message: str


@dataclass(frozen=True)
class Output:
    """A finished probe: its exit status and its output, both streams."""

    returncode: int
    text: str


# Runs a probe command for at most a number of seconds; ``None`` when the
# command could not be run or did not finish in time.
Runner = Callable[[Sequence[str], float], Output | None]


def run_probe(argv: Sequence[str], timeout: float) -> Output | None:
    """Run ``argv`` with no input and return what it printed."""
    try:
        completed = subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return Output(completed.returncode, completed.stdout + completed.stderr)


@dataclass(frozen=True)
class ModelCatalog:
    """The models an agent offers, each with its reasoning levels.

    A model's levels are ``None`` when the catalog does not say; the
    launcher's own levels then apply.
    """

    models: dict[str, tuple[str, ...] | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "models": {
                name: list(levels) if levels is not None else None
                for name, levels in self.models.items()
            }
        }

    @classmethod
    def from_dict(cls, data: object) -> ModelCatalog | None:
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, dict):
            return None
        return cls(
            {
                str(name): tuple(str(level) for level in levels)
                if isinstance(levels, list)
                else None
                for name, levels in models.items()
            }
        )


class CatalogCache:
    """Model catalogs kept per agent, binary and version under ``.ww``.

    A catalog changes with the program, so a cached one is used only while
    the same binary reports the same version.
    """

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory

    def _path(self, agent: str) -> Path | None:
        if self.directory is None:
            return None
        return self.directory / f"{re.sub(r'[^A-Za-z0-9._-]', '_', agent)}.json"

    def get(self, agent: str, binary: str, version: str) -> ModelCatalog | None:
        path = self._path(agent)
        if path is None:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (
            not isinstance(data, dict)
            or data.get("binary") != binary
            or data.get("version") != version
        ):
            return None
        return ModelCatalog.from_dict(data)

    def put(self, agent: str, binary: str, version: str, catalog: ModelCatalog) -> None:
        path = self._path(agent)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"binary": binary, "version": version, **catalog.to_dict()},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            return


@dataclass(frozen=True)
class Readiness:
    """What a probe found out about one agent's program."""

    agent: str
    binary: str | None = None
    version: str | None = None
    logged_in: bool | None = None
    catalog: ModelCatalog | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def ready(self) -> bool:
        return not any(finding.level == "error" for finding in self.findings)


@dataclass(frozen=True)
class StreamFacts:
    """What one line of a headless agent's output says, when anything."""

    session_id: str | None = None
    usage: TokenCounts | None = None
    model: str | None = None


def parse_version(text: str) -> Version | None:
    """The first dotted number in ``text``: ``2.1.300 (Claude Code)``."""
    match = re.search(r"\d+(?:\.\d+)+", text)
    return tuple(int(part) for part in match.group().split(".")) if match else None


def _dotted(version: Version) -> str:
    return ".".join(str(part) for part in version)


class HeadlessLauncher(Launcher):
    """One agent's command-line program, run without a terminal.

    Subclasses give the program's facts as class attributes and implement
    the parts that differ: logging in, the model list, and the commands.
    """

    transport = "headless"
    recording = ResultRecording.WORKER

    # The agent's name in ww, for findings and the catalog cache.
    agent: ClassVar[str]
    # The program's name on ``PATH`` when the settings give no ``binary``.
    binary: ClassVar[str]
    # Older versions are refused; newer releases than ``tested_up_to``
    # (compared on as many parts as it has) are run with a warning.
    minimum_version: ClassVar[Version]
    tested_up_to: ClassVar[Version]
    # The reasoning levels the program takes, or ``None`` when it takes no
    # reasoning setting at all.
    reasoning_levels: ClassVar[tuple[str, ...] | None]
    # The model ``cheapest`` stands for, when the agent defines one.
    cheapest_model: ClassVar[str | None] = None
    # The permission levels the program can honour while still letting the
    # worker run ``./ww`` and write ww's state.
    permission_levels: ClassVar[tuple[Permission, ...]] = ("read-only", "edit", "full")
    # Settings the program has a flag for; others are warned about.
    supported_settings: ClassVar[frozenset[str]] = frozenset()
    # Flags ww sets itself, which ``args`` should not repeat.
    owned_flags: ClassVar[frozenset[str]] = frozenset()
    # Whether the program lists its models (:meth:`catalog_command`).  A
    # model missing from the list is an error; a program with no list takes
    # the models :meth:`known_model` knows, and others with a warning.
    lists_models: ClassVar[bool] = False

    # -- the program ---------------------------------------------------------

    def resolve_binary(self, settings: HeadlessSettings) -> str | None:
        """The program's path: the configured one, else found on ``PATH``."""
        configured = settings.binary
        if configured is None:
            return shutil.which(self.binary)
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return shutil.which(configured) if os.sep not in configured else None

    def version_command(self, binary: str) -> list[str]:
        return [binary, "--version"]

    @abstractmethod
    def auth_command(self, binary: str) -> list[str]:
        """The command that reports whether the program is logged in."""

    @abstractmethod
    def logged_in(self, output: Output) -> bool | None:
        """Whether ``output`` of :meth:`auth_command` says logged in."""

    def catalog_command(self, binary: str) -> list[str] | None:
        """The command that lists the models, or ``None`` when there is none."""
        return None

    def parse_catalog(self, output: Output) -> ModelCatalog | None:
        """The models in ``output`` of :meth:`catalog_command`."""
        return None

    def known_model(self, model: str) -> bool:
        """Whether the program takes ``model`` without consulting its list."""
        return False

    def models_description(self) -> str:
        """The models :meth:`known_model` takes, in words."""
        return "none known"

    # -- commands ------------------------------------------------------------

    @abstractmethod
    def launch_command(self, binary: str, request: LaunchRequest) -> list[str]:
        """The command that starts the agent on ``request``."""

    @abstractmethod
    def resume_command(
        self, binary: str, request: LaunchRequest, session_id: str
    ) -> list[str]:
        """The command that continues session ``session_id`` with ``request``."""

    @abstractmethod
    def stream_facts(self, line: Mapping[str, Any]) -> StreamFacts:
        """What one JSON line of the agent's output says."""

    def model_arguments(self, request: LaunchRequest) -> tuple[str | None, str | None]:
        """The model and reasoning to pass, ``None`` where the flag is left out."""
        model = request.settings.model(request.model)
        if model == "cheapest":
            model = self.cheapest_model
        reasoning = request.reasoning
        return (
            None if model in {None, "auto"} else model,
            None if reasoning in {None, "auto"} else reasoning,
        )

    # -- checks --------------------------------------------------------------

    def probe(
        self,
        settings: HeadlessSettings | None = None,
        *,
        runner: Runner = run_probe,
        cache: CatalogCache | None = None,
    ) -> Readiness:
        """Find the program and its version, login and models."""
        settings = settings or HeadlessSettings()
        findings: list[Finding] = []
        binary = self.resolve_binary(settings)
        if binary is None:
            name = settings.binary or self.binary
            return Readiness(
                self.agent,
                findings=(
                    Finding(
                        "error",
                        "binary",
                        f"{name!r} was not found; install it or set its path "
                        f"under agents.{self.agent}.headless.binary",
                    ),
                ),
            )
        version_text, version_findings = self._version(binary, runner)
        findings.extend(version_findings)
        logged_in = self._login(binary, runner, findings)
        catalog = self._catalog(binary, version_text, runner, cache, findings)
        findings.extend(self.settings_findings(settings))
        return Readiness(
            self.agent,
            binary=binary,
            version=version_text,
            logged_in=logged_in,
            catalog=catalog,
            findings=tuple(findings),
        )

    def _version(self, binary: str, runner: Runner) -> tuple[str | None, list[Finding]]:
        output = runner(self.version_command(binary), PROBE_SECONDS)
        text = output.text.strip().splitlines()[0] if output and output.text else ""
        version = parse_version(text) if output and output.returncode == 0 else None
        if version is None:
            return None, [
                Finding(
                    "error",
                    "version",
                    f"{binary} --version did not report a version",
                )
            ]
        if version < self.minimum_version:
            return text, [
                Finding(
                    "error",
                    "version",
                    f"version {_dotted(version)} is older than "
                    f"{_dotted(self.minimum_version)}, the oldest ww runs headless; "
                    "update it",
                )
            ]
        if version[: len(self.tested_up_to)] > self.tested_up_to:
            return text, [
                Finding(
                    "warning",
                    "version",
                    f"version {_dotted(version)} is newer than ww has been tested "
                    f"with ({_dotted(self.tested_up_to)}); its flags may have changed",
                )
            ]
        return text, []

    def _login(
        self, binary: str, runner: Runner, findings: list[Finding]
    ) -> bool | None:
        output = runner(self.auth_command(binary), PROBE_SECONDS)
        logged_in = self.logged_in(output) if output is not None else None
        if logged_in is False:
            findings.append(
                Finding("error", "login", "not logged in; log in to it first")
            )
        elif logged_in is None:
            findings.append(
                Finding("warning", "login", "could not tell whether it is logged in")
            )
        return logged_in

    def _catalog(
        self,
        binary: str,
        version: str | None,
        runner: Runner,
        cache: CatalogCache | None,
        findings: list[Finding],
    ) -> ModelCatalog | None:
        command = self.catalog_command(binary)
        if command is None:
            return None
        if cache is not None and version is not None:
            cached = cache.get(self.agent, binary, version)
            if cached is not None:
                return cached
        output = runner(command, PROBE_SECONDS)
        catalog = self.parse_catalog(output) if output is not None else None
        if catalog is None or not catalog.models:
            findings.append(
                Finding(
                    "warning",
                    "models",
                    f"`{' '.join([Path(command[0]).name, *command[1:]])}` listed "
                    "no models; models are not checked",
                )
            )
            return None
        if cache is not None and version is not None:
            cache.put(self.agent, binary, version, catalog)
        return catalog

    def settings_findings(self, settings: HeadlessSettings) -> list[Finding]:
        """What the settings ask for that this program cannot do."""
        findings = []
        if settings.permissions not in self.permission_levels:
            findings.append(
                Finding(
                    "error",
                    "permissions",
                    f"permissions {settings.permissions!r} is not available: "
                    f"the worker could not run ./ww or write ww's state; use one "
                    f"of {', '.join(self.permission_levels)}",
                )
            )
        for name, value in (
            ("fallback_model", settings.fallback_model),
            ("max_budget_usd", settings.max_budget_usd),
            ("isolated", settings.isolated or None),
        ):
            if value is not None and name not in self.supported_settings:
                findings.append(
                    Finding(
                        "warning",
                        "settings",
                        f"{name} is not supported by this agent and is ignored",
                    )
                )
        clashes = sorted(
            {argument.split("=", 1)[0] for argument in settings.args} & self.owned_flags
        )
        if clashes:
            findings.append(
                Finding(
                    "warning",
                    "settings",
                    f"args repeat flags ww sets itself: {', '.join(clashes)}",
                )
            )
        return findings

    def selection_findings(
        self,
        model: str | None,
        reasoning: str | None,
        catalog: ModelCatalog | None = None,
        settings: HeadlessSettings | None = None,
    ) -> list[Finding]:
        """Whether the program takes ``model`` and ``reasoning``.

        Without ``catalog`` only what is known without the program is
        checked, as linting does.
        """
        settings = settings or HeadlessSettings()
        findings: list[Finding] = []
        resolved = settings.model(model)
        if resolved == "cheapest" and self.cheapest_model is None:
            findings.append(
                Finding(
                    "error",
                    "model",
                    f"{self.agent} defines no cheapest model; name one",
                )
            )
            resolved = None
        elif resolved == "cheapest":
            resolved = self.cheapest_model
        if resolved in {None, "auto"}:
            resolved = None
        levels = self.reasoning_levels
        if resolved is None or self.known_model(resolved):
            pass
        elif catalog is not None and resolved in catalog.models:
            levels = catalog.models[resolved] or levels
        elif catalog is not None:
            findings.append(
                Finding(
                    "error",
                    "model",
                    f"model {resolved!r} is not among {self.agent}'s models: "
                    f"{', '.join(sorted(catalog.models))}",
                )
            )
        elif not self.lists_models:
            findings.append(
                Finding(
                    "warning",
                    "model",
                    f"{self.agent} has no model list; {resolved!r} is not a "
                    "model ww knows, so it is passed on unchecked",
                )
            )
        if reasoning not in {None, "auto"}:
            if self.reasoning_levels is None:
                findings.append(
                    Finding(
                        "error",
                        "reasoning",
                        f"{self.agent} takes no reasoning setting; leave reasoning "
                        "out and choose a model variant that reasons as you want",
                    )
                )
            elif levels is not None and reasoning not in levels:
                about = f"model {resolved!r}" if resolved else self.agent
                findings.append(
                    Finding(
                        "error",
                        "reasoning",
                        f"reasoning {reasoning!r} is not one {about} takes: "
                        f"{', '.join(levels)}",
                    )
                )
        return findings

    def check(
        self,
        request: LaunchRequest,
        *,
        runner: Runner = run_probe,
        cache: CatalogCache | None = None,
    ) -> tuple[Readiness, tuple[Finding, ...]]:
        """Everything that must hold before ``request`` is launched."""
        readiness = self.probe(request.settings, runner=runner, cache=cache)
        findings = [
            *readiness.findings,
            *self.selection_findings(
                request.model, request.reasoning, readiness.catalog, request.settings
            ),
        ]
        return readiness, tuple(findings)
