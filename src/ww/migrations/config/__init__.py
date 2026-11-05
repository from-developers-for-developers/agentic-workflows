# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration migrations: deprecated and removed constructs, and their fixes.

A :class:`ConfigMigration` declares one construct once: where it is (a key
path), what replaces it (declarative changes, a choice between options, or
instructions for the operator), and when it was deprecated and is removed.
From that one declaration ww derives both the in-memory change it makes when
it loads an old file (:func:`migrate_loaded`) and the edits that change the
file itself (:func:`migrate_file`).

:mod:`.catalog` lists ww's own migrations in release order; an extension
declares the migrations of its settings section on its ``Extension``.
:mod:`.guide` renders the upgrade guide from the catalog.
:mod:`.project` names the files of a project to migrate; it reads the
configuration, which loads through this package, so it is imported on its
own rather than from here.
"""

from __future__ import annotations

from .catalog import CONFIG_MIGRATIONS, MigrationSet, ww_migrations
from .changes import (
    Change,
    DeleteKey,
    MigrationConflictError,
    MoveKey,
    RenameKey,
    SetValue,
    delete_key,
    move_key,
    rename_key,
    set_value,
)
from .files import (
    ConfigFile,
    FileMigration,
    migrate_file,
    migrate_files,
    write_files,
)
from .guide import render_upgrade_guide
from .loading import Migrated, migrate_loaded
from .migration import (
    FILE_KINDS,
    UPGRADE_GUIDE,
    ConfigMigration,
    FileKind,
    Finding,
    FindingKind,
    Option,
    Status,
)
from .paths import KeyPath, Location
from .yaml_text import EditRefusedError, TextEdit, YamlText

__all__ = [
    "CONFIG_MIGRATIONS",
    "FILE_KINDS",
    "UPGRADE_GUIDE",
    "Change",
    "ConfigFile",
    "ConfigMigration",
    "DeleteKey",
    "FileKind",
    "FileMigration",
    "Finding",
    "FindingKind",
    "KeyPath",
    "Location",
    "Migrated",
    "MigrationConflictError",
    "MigrationSet",
    "MoveKey",
    "Option",
    "EditRefusedError",
    "RenameKey",
    "SetValue",
    "Status",
    "TextEdit",
    "YamlText",
    "delete_key",
    "migrate_file",
    "migrate_files",
    "migrate_loaded",
    "move_key",
    "rename_key",
    "render_upgrade_guide",
    "set_value",
    "write_files",
    "ww_migrations",
]
