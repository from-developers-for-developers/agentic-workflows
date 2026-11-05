# SPDX-License-Identifier: GPL-3.0-or-later
"""The upgrade guide, generated from the configuration migrations.

Every section comes from one :class:`~.migration.ConfigMigration` of
:data:`~.catalog.CONFIG_MIGRATIONS`, in release order: its anchor, the
versions that deprecated and remove it, why, the files it appears in, what
``ww migrate`` does or what the operator chooses or changes by hand, and its
before and after excerpt when it declares one. ``ww docs upgrade`` prints
the guide, and ``ww docs upgrade#<anchor>`` one section, the anchor every
message about a construct names. ``documentation/deprecations.md`` is the
same text, written by ``scripts/generate_upgrade_guide.py``; a test keeps the
two equal, so the guide cannot drift from the migrations.

An extension's settings migrations name a guide of their own and are not
part of this one.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import catalog
from .migration import ConfigMigration, FileKind

# Where each kind of file a migration applies to is found.
_FILES: dict[FileKind, str] = {
    "workflows": (
        "workflow files (`ww.yaml`, the files it imports, `ww.local.yaml` and "
        "the user's `ww.yaml`)"
    ),
    "rules": "rule files' frontmatter",
    "settings": (
        "settings files (`ww.json`, `ww.local.json`, the user's `ww.json` and "
        "each configured project's own)"
    ),
}

_INTRO = """\
# Deprecations and upgrades

This guide lists the configuration ww still reads but is scheduled to
change, and the configuration it no longer reads. It is generated from ww's
configuration migrations: `ww docs upgrade` prints it, and
`ww docs upgrade#<section>` prints the one section that every message about
a construct names. No removal is scheduled unless a section names its
version.

ww reads a deprecated construct by migrating it in memory as it loads each
file, and warns, naming the file, the path to the construct and its line:

```text
ww warning: Deprecated: <file>:<path> (line <n>): <why> (deprecated in ww <version>); \
run ww migrate to fix. See ww docs upgrade#<section>.
```

Imported, user-level, local and inherited configuration is included, and so
are rule files' frontmatter and the settings files. `ww lint` lists every
construct left, marked `[auto-fixable by ww migrate]`, `[choose with ww
migrate]` or `[manual]`. Once a construct is removed, a file still holding it
is an error naming the removal and its section here. Using both the old and
the new spelling in one place is an error.

## Upgrading

1. `ww migrate` shows what it would change: every construct it finds, with
   its file and line, and a diff of every file it would edit. It changes
   nothing.
2. `ww migrate --apply` makes the automatic changes, editing each file in
   place so its comments and formatting stay. A construct with several
   options is changed once you choose one: at a terminal `ww migrate --apply`
   asks; elsewhere pass `--choose <migration>=<option>`. ww never chooses for
   you.
3. What ww cannot change is left to you. `ww migrate` names each place, why
   it changed, the options and their consequences, what to edit, and its
   section below.
4. `ww migrate --check` exits non-zero while a removed construct is left, and
   `ww migrate --check --deprecations` while anything is.

Stored task state needs no migration: ww reads what an older ww wrote.
`ww migrate --tasks --apply` rewrites it in the current format anyway, after
copying it to `.ww/backups/`.
"""


def render_upgrade_guide(migrations: Sequence[ConfigMigration] | None = None) -> str:
    """The upgrade guide for ``migrations``, by default ww's own, as Markdown."""
    migrations = catalog.CONFIG_MIGRATIONS if migrations is None else migrations
    sections = [render_section(migration) for migration in migrations]
    if not sections:
        sections.append("Nothing is deprecated or removed today.\n")
    return "\n".join([_INTRO, *sections])


def render_section(migration: ConfigMigration) -> str:
    """One migration's section of the guide, under its anchor."""
    removal = (
        f"removed in ww {migration.removed_in}"
        if migration.removed_in is not None
        else "no removal is scheduled"
    )
    found = " and ".join(_FILES[kind] for kind in migration.files)
    parts = [
        f'<a id="{migration.anchor}"></a>\n',
        f"## {migration.title or migration.id}\n",
        f"Migration `{migration.id}`: deprecated in ww {migration.deprecated_in}; "
        f"{removal}. Found in {found}.\n",
        f"Why: {_sentence(migration.why)}\n",
    ]
    if migration.notes:
        parts.append(f"{migration.notes}\n")
    parts.append(_remedy(migration))
    if migration.example is not None:
        language = "json" if migration.files == ("settings",) else "yaml"
        before, after = migration.example
        parts.append(f"Before:\n\n```{language}\n{before.rstrip()}\n```\n")
        parts.append(f"After:\n\n```{language}\n{after.rstrip()}\n```\n")
    return "\n".join(parts)


def _remedy(migration: ConfigMigration) -> str:
    match migration.kind:
        case "automatic":
            return "Automatic: `ww migrate --apply` makes the change:\n\n" + "".join(
                f"- {change.describe()}\n" for change in migration.changes
            )
        case "choice":
            return (
                "Choice: `ww migrate --apply` asks which option to apply, or "
                f"takes `--choose {migration.id}=<option>`. Until you choose, "
                "ww reads it as the first option.\n\n"
                + "".join(
                    f"- `{option.name}`: {_sentence(option.consequence)} ww will "
                    + "; ".join(change.describe() for change in option.changes)
                    + ".\n"
                    for option in migration.options
                )
            )
        case _:
            return "Manual: change it by hand, in one of these ways:\n\n" + "".join(
                f"- `{option.name}`: {_sentence(option.consequence)}\n"
                for option in migration.options
            )


def _sentence(text: str) -> str:
    """``text`` with a capital first letter and a final full stop."""
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "!", "?")) else f"{text}."
