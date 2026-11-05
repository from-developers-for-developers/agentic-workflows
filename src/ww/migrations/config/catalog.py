# SPDX-License-Identifier: GPL-3.0-or-later
"""Every configuration migration ww has, in the order releases added them.

A migration joins the end of :data:`CONFIG_MIGRATIONS` in the release that
deprecates its construct, and is applied after every one before it, so a
file several releases old is brought forward one release at a time. It
stays until the next major version after its ``removed_in``, when its
detector has done its job and it can be deleted.

An extension declares its settings migrations on its :class:`Extension` as
``settings_migrations``; they are judged against the extension's version and
applied inside its ``extensions.<id>`` section (see
:meth:`ww.extensions.ExtensionRegistry.settings_migrations`).
"""

from __future__ import annotations

from dataclasses import dataclass

from ww import __version__

from .changes import rename_key
from .migration import ConfigMigration, FileKind

# ``assess.question`` became ``assess.assess`` (1.0.0b12): in an assessment
# mapping, standalone or attached to a step, and in the explicit
# ``name: assess`` step. Steps nest anywhere (steps, hooks, handlers, items,
# children, assessment branches), so both paths look at every depth.
ASSESS_QUESTION = ConfigMigration(
    id="assess-question",
    deprecated_in="1.0.0b12",
    why="an assessment's prompt key question is now assess",
    anchor="assessment-prompt-key-question",
    files=("workflows",),
    changes=(
        rename_key("**.assess.question", "assess"),
        rename_key("**[name=assess].question", "assess"),
    ),
    title="Assessment prompt key `question`",
    notes=(
        "Use `assess` inside the full assessment mapping. The compact "
        "`assess: <prompt>` form remains supported. The same applies inside "
        "an assessment attached to a regular step, and to the explicit "
        "standalone `name: assess` form, whose `question` field becomes "
        "`assess` too."
    ),
    example=(
        "- assess:\n"
        "    question: Are there issues?\n"
        "    positive:\n"
        "      handler: fix-findings\n",
        "- assess:\n"
        "    assess: Are there issues?\n"
        "    positive:\n"
        "      handler: fix-findings\n",
    ),
)

CONFIG_MIGRATIONS: tuple[ConfigMigration, ...] = (ASSESS_QUESTION,)


@dataclass(frozen=True)
class MigrationSet:
    """Migrations judged against one version: ww's own, or an extension's.

    ``owner`` names that version's software in messages: ``ww``, or an
    extension's identifier.
    """

    migrations: tuple[ConfigMigration, ...]
    version: str
    owner: str = "ww"


def ww_migrations(kind: FileKind) -> MigrationSet:
    """ww's own migrations for files of ``kind``, in release order."""
    return MigrationSet(
        tuple(migration for migration in CONFIG_MIGRATIONS if kind in migration.files),
        __version__,
    )
