# SPDX-License-Identifier: GPL-3.0-or-later
"""``ww visualize``: a local, read-only page of the project's workflows and tasks.

This package is an extra on top of the engine, not part of it. It reads
everything through the core: the configuration as ``load_configuration``
resolves it, each workflow's plan as ``compile_for_any_agent`` compiles it,
and tasks, runs, artifacts and rule outcomes through the task store and the
service. What it adds is only the view: the JSON the page renders, the page
itself, and a small server that tells the page when a watched file changed.
It never writes ww state.
"""

from .server import serve_visualizer

__all__ = ["serve_visualizer"]
