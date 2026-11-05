# SPDX-License-Identifier: GPL-3.0-or-later
"""Every older shape ww still reads, kept out of the engine.

The engine and its models only ever see the current shape of what they
load. A file written by an older ww is brought up to date here, before any
decoder sees it, so no reader branches on an old shape:
:mod:`ww.migrations.data` upgrades persisted data (task state, the rule
stores, feedback and run reports) at load time, and
:mod:`ww.migrations.config` brings deprecated configuration to the current
shape in memory as it loads, and in the files under ``ww migrate``.
:mod:`ww.migrations.tasks` rewrites stored task state in the current format
for ``ww migrate --tasks``; it imports the storage adapter, so the package
does not import it.
"""
