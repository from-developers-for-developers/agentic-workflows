# SPDX-License-Identifier: GPL-3.0-or-later
"""The operator page: a local web page the operator answers items on.

This package is an extra on top of the engine, not part of it.  The core
knows one thing about it: a substep inside an items context may be declared
``interactive: page``, and the step's page then tells the agent to run
``interact --await``.  Everything
else lives here.  The page is served for exactly as long as that command
waits; answers wait in a file of their own, and once every item of the
context is answered they are recorded with ``interact`` as the operator's
evidence and the interaction ends.  Applying fields and the explicit
resolve/report transitions, and completing the step, stay the agent's work.
"""

from .session import OperatorPageResult, run_operator_page

__all__ = ["OperatorPageResult", "run_operator_page"]
