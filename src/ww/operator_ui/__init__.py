# SPDX-License-Identifier: GPL-3.0-or-later
"""The operator page: a local web page the operator answers items on.

This package is an extra on top of the engine, not part of it.  The core
knows one thing about it: a per-item stage may be declared
``interactive: page``, and the step's page then tells the agent to run
``interact --await``.  Everything
else lives here.  The page is served for exactly as long as that command
waits; answers wait in a file of their own, and once the wait is over they
are applied through the same public service calls an agent would make:
``interact``, ``update_item``, ``complete``, and ``next``.
"""

from .session import OperatorPageResult, run_operator_page

__all__ = ["OperatorPageResult", "run_operator_page"]
