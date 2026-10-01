# SPDX-License-Identifier: GPL-3.0-or-later
"""Lazy discovery and validated lookup of installed ww extensions.

Discovery records where an extension comes from without importing its Python
module. Executable code is loaded only when a caller asks for that extension's
handlers, modes, or commands. Entry-point names are metadata and must use the
``vendor.name`` form for an extension identified as ``vendor/name``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from importlib.resources import as_file, files
from pathlib import Path

from ww.errors import ConfigurationError
from ww.extensions.api import (
    EXTENSION_API_VERSION,
    Extension,
    ExtensionCommand,
    ExtensionContext,
    ExtensionHandler,
    ExtensionNamespace,
    RuleGroupContribution,
)
from ww.extensions.store import ExtensionStore
from ww.project_config import (
    FILE_NAME,
    ProjectConfig,
    ProjectSettings,
    load_project_config,
    overlay_settings,
)
from ww.variables import OVERRIDABLE_CORE_VARIABLE_NAMES, namespaced
from ww.workflow_config import ModeDefinition

ENTRY_POINT_GROUP = "ww.extensions"
EXTENSION_DIRECTORY = "ext"
# The vendor of the extensions ww bundles; a project may not provide them.
BUNDLED_VENDOR = "ww"
SECTIONS = ("handlers", "modes")

# One lower-case vendor or extension name, e.g. "ww" or "pull_request".
_SEGMENT = r"[a-z0-9][a-z0-9_-]*"
# An extension identifier, e.g. "ww/git".
_IDENTIFIER = re.compile(rf"^(?P<vendor>{_SEGMENT})/(?P<name>{_SEGMENT})$")
# An entry point name, e.g. "ww.git".
_ENTRY_POINT_NAME = re.compile(rf"^(?P<vendor>{_SEGMENT})\.(?P<name>{_SEGMENT})$")
# An extension item reference, e.g. "ext/ww/git/handlers:commit".
_REFERENCE = re.compile(
    rf"^ext/(?P<vendor>{_SEGMENT})/(?P<name>{_SEGMENT})"
    r"/(?P<section>[a-z]+):(?P<item>[A-Za-z0-9][A-Za-z0-9_.-]*)$"
)


@dataclass(frozen=True)
class ExtensionReference:
    """A parsed ``ext/<vendor>/<name>/<section>:<item>`` reference."""

    vendor: str
    name: str
    section: str
    item: str

    @property
    def identifier(self) -> str:
        return f"{self.vendor}/{self.name}"

    def __str__(self) -> str:
        return f"ext/{self.identifier}/{self.section}:{self.item}"


@dataclass(frozen=True)
class ExtensionIdentity:
    """The executable identity frozen into a compiled workflow plan."""

    identifier: str
    version: str
    api_version: int
    source: str
    fingerprint: str


@dataclass
class _ExtensionProvider:
    identifier: str
    source: str
    fingerprint: str
    loader: Callable[[], Extension]
    loaded: Extension | None = None


def is_extension_reference(value: str) -> bool:
    """Return whether ``value`` is addressed to an extension."""
    return value.startswith("ext/")


def parse_reference(value: str) -> ExtensionReference:
    match = _REFERENCE.match(value)
    if match is None:
        raise ConfigurationError(
            f"invalid extension reference {value!r}; expected "
            "ext/<vendor>/<name>/<section>:<item>"
        )
    reference = ExtensionReference(**match.groupdict())
    if reference.section not in SECTIONS:
        raise ConfigurationError(
            f"unknown extension section {reference.section!r} in {value!r}; "
            f"choose one of: {', '.join(SECTIONS)}"
        )
    return reference


class ExtensionRegistry:
    """The lazily loaded extensions available to one project root."""

    def __init__(
        self,
        root: Path,
        extensions: tuple[Extension, ...] = (),
        config: ProjectConfig | None = None,
    ) -> None:
        self.root = Path(root)
        self._config = config
        self._project_settings: dict[str, ProjectSettings] = {}
        self._providers: dict[str, _ExtensionProvider] = {}
        for extension in extensions:
            self._add_provider(
                _ExtensionProvider(
                    extension.identifier,
                    "in-process",
                    _fingerprint(
                        f"{extension.identifier}:{extension.version}:"
                        f"{extension.api_version}"
                    ),
                    _extension_loader(extension),
                )
            )

    @classmethod
    def discover(cls, root: Path) -> ExtensionRegistry:
        root = Path(root)
        registry = cls(root)
        for provider in _bundled_providers():
            registry._add_provider(provider)
        for provider in _project_providers(root):
            registry._add_provider(provider)
        for provider in _installed_providers():
            registry._add_provider(provider)
        return registry

    @property
    def identifiers(self) -> tuple[str, ...]:
        """Return discovered identifiers without importing extension code."""
        return tuple(sorted(self._providers))

    def validate_configuration(self, project: str | None = None) -> None:
        """Check the root's sections, and a project's own when one is named."""
        self.config.validate_against(self.identifiers)
        if project is not None:
            self.project_settings(project).validate_against(self.identifiers)

    @property
    def config(self) -> ProjectConfig:
        if self._config is None:
            self._config = load_project_config(self.root / FILE_NAME)
        return self._config

    def project_settings(self, project: str) -> ProjectSettings:
        """What a configured project's own settings files contribute, read once."""
        loaded = self._project_settings.get(project)
        if loaded is None:
            loaded = self.config.project_settings(self.root, project)
            self._project_settings[project] = loaded
        return loaded

    def task_format(self, project: str | None = None) -> str | None:
        """The generated task ID format: the project's own, else the root's."""
        if project is not None:
            own = self.project_settings(project).task_format
            if own is not None:
                return own
        return self.config.task_format

    def settings(
        self, identifier: str, project: str | None = None
    ) -> dict[str, object]:
        """Return a detached copy of one extension's validated settings.

        With ``project``, that project's own section for the extension is
        applied over the root's, so work done in the project follows the
        project's conventions while the root stays the only place that
        decides which extensions are configured at all.
        """
        self.get(identifier)
        self.validate_configuration(project)
        settings = self.config.settings_for(identifier)
        if project is not None:
            settings = overlay_settings(
                settings,
                self.project_settings(project).sections.settings_for(identifier),
            )
        try:
            json.dumps(settings, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(
                f"settings for extension {identifier!r} must be JSON serializable"
            ) from error
        return settings

    @property
    def extensions(self) -> tuple[Extension, ...]:
        """Load all extensions for an explicit full-catalog request."""
        return tuple(self.get(identifier) for identifier in self.identifiers)

    def get(self, identifier: str) -> Extension:
        provider = self._providers.get(identifier)
        if provider is None:
            raise ConfigurationError(
                f"unknown extension {identifier!r}; {self._available()}"
            )
        if provider.loaded is None:
            provider.loaded = self._load(provider)
        return provider.loaded

    def identity(self, identifier: str) -> ExtensionIdentity:
        extension = self.get(identifier)
        provider = self._providers[identifier]
        return ExtensionIdentity(
            identifier,
            extension.version,
            extension.api_version,
            provider.source,
            provider.fingerprint,
        )

    def validate_identity(
        self,
        identifier: str,
        *,
        version: str | None,
        api_version: int | None,
        source: str | None,
        fingerprint: str | None,
    ) -> None:
        """Reject dispatch through an extension other than the one a plan used.

        Identity is the extension's version, API version, and provider source.
        The source fingerprint is recorded for audit but not enforced: like
        ww's own code, an extension may be fixed in place while a task is in
        flight, and a change in behaviour is signalled by a version bump.
        """
        del fingerprint
        if all(value is None for value in (version, api_version, source)):
            return
        current = self.identity(identifier)
        expected = (version, api_version, source)
        actual = (current.version, current.api_version, current.source)
        if expected != actual:
            raise ConfigurationError(
                f"extension {identifier!r} no longer matches the saved plan; "
                f"expected version {version!r} (API {api_version}) from "
                f"{source!r}, found version {current.version!r} "
                f"(API {current.api_version}) from {current.source!r}"
            )

    def handler(self, value: str) -> ExtensionHandler:
        reference = self._resolve(value, "handlers")
        self._require_reference_provider(value, reference.identifier)
        extension = self.get(reference.identifier)
        handler = extension.handlers_by_name.get(reference.item)
        if handler is None:
            raise ConfigurationError(
                f"extension {reference.identifier!r} has no handler "
                f"{reference.item!r}; available: "
                + (", ".join(sorted(extension.handlers_by_name)) or "none")
            )
        return handler

    def mode(self, value: str) -> ModeDefinition:
        reference = self._resolve(value, "modes")
        self._require_reference_provider(value, reference.identifier)
        extension = self.get(reference.identifier)
        mode = extension.modes_by_name.get(reference.item)
        if mode is None:
            raise ConfigurationError(
                f"extension {reference.identifier!r} has no mode "
                f"{reference.item!r}; available: "
                + (", ".join(sorted(extension.modes_by_name)) or "none")
            )
        return ModeDefinition(str(reference), mode.description)

    def command(self, identifier: str, name: str) -> ExtensionCommand:
        extension = self.get(identifier)
        command = extension.commands_by_name.get(name)
        if command is None:
            raise ConfigurationError(
                f"extension {identifier!r} has no command {name!r}; available: "
                + (", ".join(sorted(extension.commands_by_name)) or "none")
            )
        return command

    def qualified_modes(self) -> tuple[ModeDefinition, ...]:
        """Load and return every extension mode under its qualified name."""
        return tuple(
            ModeDefinition(
                f"ext/{extension.identifier}/modes:{mode.name}", mode.description
            )
            for extension in self.extensions
            for mode in extension.modes
        )

    def apply_variable_overrides(
        self,
        values: dict[str, str],
        bindings: tuple[tuple[str, Mapping[str, object] | None], ...],
        *,
        task_id: str,
        run_id: str | None,
        workflow: str,
        workflow_values: dict[str, str],
        workspace: Path | None,
    ) -> dict[str, str]:
        """Apply referenced extensions' overrides to existing core variables."""
        result = dict(values)
        claimed: dict[str, str] = {}
        for identifier, frozen_settings in dict(bindings).items():
            extension = self.get(identifier)
            extension_context = ExtensionContext(
                root=self.root,
                store=self.store(identifier),
                config=(
                    deepcopy(dict(frozen_settings))
                    if frozen_settings is not None
                    else self.settings(identifier)
                ),
                task_id=task_id,
                run_id=run_id,
                workflow=workflow,
                values={**workflow_values, **values},
                workspace=workspace,
            )
            for variable in extension.variables:
                if variable.name not in OVERRIDABLE_CORE_VARIABLE_NAMES:
                    raise ConfigurationError(
                        f"extension {identifier!r} cannot override core variable "
                        f"{variable.name!r}"
                    )
                previous = claimed.get(variable.name)
                if previous is not None:
                    raise ConfigurationError(
                        f"extensions {previous!r} and {identifier!r} both override "
                        f"core variable {variable.name!r}"
                    )
                try:
                    value = variable.resolve(extension_context)
                except ConfigurationError:
                    raise
                except Exception as error:  # noqa: BLE001 - add extension context
                    raise ConfigurationError(
                        f"extension {identifier!r} failed to resolve variable "
                        f"{variable.name!r}: {error}"
                    ) from error
                if value is not None and not isinstance(value, str):
                    raise ConfigurationError(
                        f"extension {identifier!r} variable {variable.name!r} "
                        "must resolve to a string or null"
                    )
                claimed[variable.name] = identifier
                if value is not None:
                    result[variable.name] = value
        return result

    def rule_groups(self) -> tuple[tuple[str, RuleGroupContribution], ...]:
        """The rule groups of the extensions the root lists, with their IDs.

        Only an extension with a section in the root settings, even an empty
        one, contributes rules, so installing one never changes what a
        project's steps are told; loading those is the cost of asking.
        """
        return tuple(
            (identifier, group)
            for identifier in self.identifiers
            if self.config.sections.lists(identifier)
            for group in self.get(identifier).rules
        )

    def namespaces(self) -> dict[str, tuple[str, ExtensionNamespace]]:
        """The template namespaces of the extensions the root lists, by name.

        Like rule groups, a namespace comes with listing the extension in the
        root settings, even with an empty section; two listed extensions
        claiming one namespace are an error.
        """
        claimed: dict[str, tuple[str, ExtensionNamespace]] = {}
        for identifier in self.identifiers:
            if not self.config.sections.lists(identifier):
                continue
            namespace = self.get(identifier).namespace
            if namespace is None:
                continue
            previous = claimed.get(namespace.name)
            if previous is not None:
                raise ConfigurationError(
                    f"extensions {previous[0]!r} and {identifier!r} both provide "
                    f"the template namespace {namespace.name!r}"
                )
            claimed[namespace.name] = (identifier, namespace)
        return claimed

    def namespace_variables(self) -> tuple[str, ...]:
        """Every ``ww.<namespace>.<variable>`` name a template may reference."""
        return tuple(
            namespaced(name, variable.name)
            for name, (_, namespace) in self.namespaces().items()
            for variable in namespace.variables
        )

    def namespace_values(
        self,
        names: tuple[str, ...],
        *,
        task_id: str,
        run_id: str | None,
        workflow: str,
        workflow_values: Mapping[str, str],
        workspace: Path | None,
        project: str | None,
    ) -> dict[str, str]:
        """Resolve the namespaced ``names`` for ``task_id``.

        A name no listed namespace provides, or one whose resolver returns
        ``None`` (not available for the task yet), is left out: an agent
        step reading it stops for the operator before it starts, and an
        automatic handler reading it fails.
        """
        wanted = set(names)
        values: dict[str, str] = {}
        for name, (identifier, namespace) in self.namespaces().items():
            variables = [
                variable
                for variable in namespace.variables
                if namespaced(name, variable.name) in wanted
            ]
            if not variables:
                continue
            context = ExtensionContext(
                root=self.root,
                store=self.store(identifier),
                config=self.settings(identifier, project),
                task_id=task_id,
                run_id=run_id,
                workflow=workflow,
                values=dict(workflow_values),
                workspace=workspace,
            )
            for variable in variables:
                full_name = namespaced(name, variable.name)
                try:
                    value = variable.resolve(context)
                except ConfigurationError:
                    raise
                except Exception as error:  # noqa: BLE001 - add extension context
                    raise ConfigurationError(
                        f"extension {identifier!r} failed to resolve "
                        f"{full_name!r}: {error}"
                    ) from error
                if value is not None and not isinstance(value, str):
                    raise ConfigurationError(
                        f"extension {identifier!r} variable {full_name!r} "
                        "must resolve to a string or null"
                    )
                if value is not None:
                    values[full_name] = value
        return values

    def configured(self, project: str | None = None) -> tuple[str, ...]:
        """The extensions with a settings section at the root or in ``project``.

        Those are the ones the project has chosen, and loading them is the
        cost of asking.
        """
        return tuple(
            identifier
            for identifier in self.identifiers
            if self.config.settings_for(identifier)
            or (
                project is not None
                and self.project_settings(project).sections.settings_for(
                    identifier
                )
            )
        )

    def reserved_paths(
        self, task_id: str, workflow: str | None, project: str | None = None
    ) -> tuple[Path, ...]:
        """Paths configured extensions claim for ``task_id`` outside ww state."""
        paths: list[Path] = []
        for identifier in self.configured(project):
            extension = self.get(identifier)
            if extension.reserved_paths is None:
                continue
            context = ExtensionContext(
                root=self.root,
                store=self.store(identifier),
                config=self.settings(identifier, project),
                task_id=task_id,
                workflow=workflow,
                workspace=(
                    self.config.projects_by_name[project].directory(self.root)
                    if project is not None
                    else None
                ),
            )
            try:
                claimed = extension.reserved_paths(context)
            except ConfigurationError:
                raise
            except Exception as error:  # noqa: BLE001 - add extension context
                raise ConfigurationError(
                    f"extension {identifier!r} failed to report reserved paths: {error}"
                ) from error
            paths.extend(Path(path) for path in claimed)
        return tuple(paths)

    def branch_strategies(self, project: str | None = None) -> tuple[str, ...]:
        """Names configured extensions accept for ``start --branch-strategy``."""
        names: list[str] = []
        for identifier in self.configured(project):
            extension = self.get(identifier)
            if extension.branch_strategies is None:
                continue
            try:
                declared = extension.branch_strategies(
                    self.settings(identifier, project)
                )
            except ConfigurationError:
                raise
            except Exception as error:  # noqa: BLE001 - add extension context
                raise ConfigurationError(
                    f"extension {identifier!r} failed to report branch strategies: "
                    f"{error}"
                ) from error
            names.extend(declared)
        return tuple(dict.fromkeys(names))

    def store(self, identifier: str) -> ExtensionStore:
        self.get(identifier)
        return ExtensionStore(self.root, identifier)

    def _resolve(self, value: str, section: str) -> ExtensionReference:
        reference = parse_reference(value)
        if reference.section != section:
            raise ConfigurationError(
                f"{value!r} addresses {reference.section!r}, but a "
                f"{section[:-1]} is required here"
            )
        return reference

    def _add_provider(self, provider: _ExtensionProvider) -> None:
        if not _IDENTIFIER.fullmatch(provider.identifier):
            raise ConfigurationError(
                "invalid extension identifier in discovery metadata: "
                f"{provider.identifier!r}"
            )
        if provider.identifier in self._providers:
            raise ConfigurationError(
                f"extension {provider.identifier!r} is provided more than once"
            )
        self._providers[provider.identifier] = provider

    def _require_reference_provider(self, value: str, identifier: str) -> None:
        if identifier not in self._providers:
            raise ConfigurationError(
                f"{value!r} references unknown extension {identifier!r}; "
                f"{self._available()}"
            )

    def _load(self, provider: _ExtensionProvider) -> Extension:
        try:
            extension = provider.loader()
        except ConfigurationError:
            raise
        except Exception as error:  # noqa: BLE001 - add provider context
            raise ConfigurationError(
                f"extension {provider.source} failed to load: {error}"
            ) from error
        if not isinstance(extension, Extension):
            raise ConfigurationError(
                f"extension {provider.source} must resolve to an Extension"
            )
        if extension.identifier != provider.identifier:
            raise ConfigurationError(
                f"extension at {provider.source} declares {extension.identifier!r} "
                f"but discovery metadata says {provider.identifier!r}"
            )
        if extension.api_version != EXTENSION_API_VERSION:
            raise ConfigurationError(
                f"extension {extension.identifier!r} requires API version "
                f"{extension.api_version}; ww supports {EXTENSION_API_VERSION}"
            )
        return extension

    def _available(self) -> str:
        names = ", ".join(self.identifiers)
        return f"available: {names}" if names else "no extensions are installed"


def _project_providers(root: Path) -> tuple[_ExtensionProvider, ...]:
    directory = root / EXTENSION_DIRECTORY
    if not directory.is_dir():
        return ()
    found = []
    ww_checkout = is_ww_checkout(root)
    for module_path in sorted(directory.glob("*/*/extension.py")):
        if module_path.resolve() == _bundled_source_path().resolve():
            continue
        name, vendor = module_path.parent.name, module_path.parent.parent.name
        if ww_checkout and vendor == BUNDLED_VENDOR:
            # Another checkout of ww itself: its ext/ww is a copy of ww's own
            # bundled extensions, possibly another version, not something the
            # project adds. The running install's bundled copy is used.
            continue
        identifier = f"{vendor}/{name}"
        relative = module_path.relative_to(root).as_posix()
        found.append(
            _ExtensionProvider(
                identifier,
                f"project:{relative}",
                _file_fingerprint(module_path),
                _module_loader(module_path, identifier),
            )
        )
    return tuple(found)


def _bundled_providers() -> tuple[_ExtensionProvider, ...]:
    resource = (
        files("ww")
        .joinpath("_bundled_extensions")
        .joinpath("ww")
        .joinpath("git")
        .joinpath("extension.py")
    )
    if resource.is_file():
        contents = resource.read_bytes()

        def load_bundled() -> Extension:
            with as_file(resource) as path:
                return _load_module(path, "ww/git")

        return (
            _ExtensionProvider(
                "ww/git", "bundled:ww/git", _bytes_fingerprint(contents), load_bundled
            ),
        )
    source = _bundled_source_path()
    if source.is_file():
        return (
            _ExtensionProvider(
                "ww/git",
                "bundled:ww/git",
                _file_fingerprint(source),
                lambda: _load_module(source, "ww/git"),
            ),
        )
    return ()


def is_ww_checkout(root: Path) -> bool:
    """Whether ``root`` is a source checkout of ww itself.

    Two checkouts of ww may sit side by side, one for developing ww and one
    kept on ``dev``, each with its own install; either install then runs in
    the other checkout. The test is one file only ww's source tree holds.
    """
    return (root / "src" / "ww" / "extensions" / "registry.py").is_file()


def _bundled_source_path() -> Path:
    return Path(__file__).parents[3] / "ext" / "ww" / "git" / "extension.py"


def _load_module(path: Path, identifier: str) -> Extension:
    module_name = f"ww_ext_{identifier.replace('/', '_').replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ConfigurationError(f"cannot load extension module: {path}")
    module = importlib.util.module_from_spec(spec)
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:  # noqa: BLE001 - reported with its own context
        sys.modules.pop(module_name, None)
        raise ConfigurationError(
            f"extension {path} failed to import: {error}"
        ) from error
    finally:
        sys.dont_write_bytecode = written
    extension = getattr(module, "EXTENSION", None)
    if not isinstance(extension, Extension):
        raise ConfigurationError(f"extension {path} must define EXTENSION")
    return extension


def _installed_providers() -> tuple[_ExtensionProvider, ...]:
    found = []
    for point in entry_points(group=ENTRY_POINT_GROUP):
        match = _ENTRY_POINT_NAME.fullmatch(point.name)
        if match is None:
            raise ConfigurationError(
                f"extension entry point {point.name!r} must use vendor.name"
            )
        identifier = f"{match['vendor']}/{match['name']}"
        distribution = getattr(point, "dist", None)
        distribution_name = getattr(distribution, "name", None) or "unknown-package"
        distribution_version = getattr(distribution, "version", None) or "unknown"
        source = f"package:{distribution_name}@{distribution_version}:{point.value}"
        found.append(
            _ExtensionProvider(
                identifier,
                source,
                _fingerprint(source),
                _entry_point_loader(point),
            )
        )
    return tuple(found)


def _load_entry_point(point: EntryPoint) -> Extension:
    loaded = point.load()
    extension = loaded() if callable(loaded) else loaded
    if not isinstance(extension, Extension):
        raise ConfigurationError(
            f"entry point {point.name!r} must resolve to an Extension"
        )
    return extension


def _extension_loader(extension: Extension) -> Callable[[], Extension]:
    return lambda: extension


def _module_loader(path: Path, identifier: str) -> Callable[[], Extension]:
    return lambda: _load_module(path, identifier)


def _entry_point_loader(point: EntryPoint) -> Callable[[], Extension]:
    return lambda: _load_entry_point(point)


def _file_fingerprint(path: Path) -> str:
    try:
        return _bytes_fingerprint(path.read_bytes())
    except OSError as error:
        raise ConfigurationError(
            f"cannot read extension source {path}: {error}"
        ) from error


def _bytes_fingerprint(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _fingerprint(value: str) -> str:
    return _bytes_fingerprint(value.encode())
