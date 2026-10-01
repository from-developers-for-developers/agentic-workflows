# SPDX-License-Identifier: GPL-3.0-or-later
"""The local HTTP server behind the operator page.

It is not a daemon: it runs for exactly as long as one wait, on a port fixed
per task so the browser tab survives between waits and reconnects by itself.
It holds no state of its own.  ``state`` is asked for every refresh and
``act`` for every operator action; both belong to the session, which keeps
the answer sheet.  The wait ends when an action says so, when the tab says
it is closing and no tab polls again within a moment, or when the timeout
passes.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Literal
from urllib.parse import urlsplit

from ww.agents import WAIT_VARIABLE
from ww.errors import StateError, WwError

PORT_VARIABLE = "WW_OPERATOR_PORT"
DEFAULT_WAIT_SECONDS = 90.0
# The lowest and the number of ports a task's page is hashed into.
_PORT_BASE = 40000
_PORT_SPAN = 10000
# How long a fresh waiter listens for an already open tab before opening one.
_OPEN_BROWSER_AFTER = 2.0
# How long after a tab says it is closing a poll still counts as the same
# operator: a reload sends the same signal as a close.
_CLOSING_GRACE = 3.0
# After the last item is answered the wait stays a moment, so a comment the
# operator is still typing for it lands before the answers are applied.
_SETTLE = 8.0
_BIND_RETRY_SECONDS = 3.0
_BIND_RETRY_INTERVAL = 0.2

WaitOutcome = Literal["answered", "paused", "closed", "timed_out"]
StateProvider = Callable[[], dict[str, object]]
# Applies one operator action; returns why the wait is over, or None.
Action = Callable[[dict[str, object]], WaitOutcome | None]


def operator_page_port(task_id: str) -> int:
    """The page's port: ``WW_OPERATOR_PORT``, else one derived from the task."""
    configured = os.environ.get(PORT_VARIABLE)
    if configured:
        try:
            return int(configured)
        except ValueError:
            raise StateError(f"{PORT_VARIABLE} must be a port number") from None
    digest = hashlib.sha256(task_id.encode("utf-8")).digest()
    return _PORT_BASE + int.from_bytes(digest[:4], "big") % _PORT_SPAN


def operator_wait_seconds() -> float:
    """How long one wait lasts: ``WW_OPERATOR_WAIT``, else the default."""
    configured = os.environ.get(WAIT_VARIABLE)
    if not configured:
        return DEFAULT_WAIT_SECONDS
    try:
        return float(configured)
    except ValueError:
        raise StateError(f"{WAIT_VARIABLE} must be a number of seconds") from None


def operator_page_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def page_html() -> str:
    return files("ww.operator_ui").joinpath("page.html").read_text("utf-8")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, state: StateProvider, act: Action, html: str) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.state = state
        self.act = act
        self.html = html
        self.outcome: WaitOutcome | None = None
        self.seen = threading.Event()
        self.polled = threading.Event()
        self.closing = threading.Event()
        self.wake = threading.Event()


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """The page is served to a person; its request log has no reader."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, self.server.html.encode("utf-8"), "text/html")
        elif path == "/state":
            self.server.seen.set()
            self.server.polled.set()
            self._send_json(200, self.server.state())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        path = urlsplit(self.path).path
        if path == "/closing":
            self._send_json(200, {"ok": True})
            self.server.closing.set()
            self.server.wake.set()
            return
        if path != "/act":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("the action must be an object")
            outcome = self.server.act(payload)
        except (ValueError, WwError) as error:
            self._send_json(400, {"error": str(error)})
            return
        except Exception as error:  # noqa: BLE001 - the page must hear of it
            self._send_json(500, {"error": f"ww could not record that: {error}"})
            return
        self._send_json(200, {"ok": True})
        if outcome is not None:
            self.server.outcome = outcome
            self.server.wake.set()

    def _send_json(self, status: int, body: dict[str, object]) -> None:
        self._send(status, json.dumps(body).encode("utf-8"), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _bind(port: int, state: StateProvider, act: Action, html: str) -> _Server:
    """Bind the page's port, waiting briefly for a previous waiter to let go."""
    deadline = time.monotonic() + _BIND_RETRY_SECONDS
    while True:
        try:
            return _Server(port, state, act, html)
        except OSError as error:
            if error.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                raise StateError(
                    f"the operator page cannot listen on port {port}: {error}; "
                    f"set {PORT_VARIABLE} to a free port"
                ) from error
            time.sleep(_BIND_RETRY_INTERVAL)


def serve_operator_page(
    *,
    port: int,
    state: StateProvider,
    act: Action,
    timeout: float,
    open_browser: Callable[[str], object] | None,
) -> WaitOutcome:
    """Serve the page for one wait and say how the wait ended.

    A tab that is already open polls the page, so the browser is opened only
    when nobody asks for the state within a moment of the page coming up.
    """
    server = _bind(port, state, act, page_html())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    try:
        if not server.seen.wait(min(_OPEN_BROWSER_AFTER, timeout)) and open_browser:
            open_browser(operator_page_url(port))
        return _wait(server, deadline)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _wait(server: _Server, deadline: float) -> WaitOutcome:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not server.wake.wait(remaining):
            return "timed_out"
        server.wake.clear()
        if server.outcome == "answered":
            if server.wake.wait(min(_SETTLE, max(deadline - time.monotonic(), 0))):
                continue  # more came in; look again
            return "answered"
        if server.outcome is not None:
            return server.outcome
        if server.closing.is_set():
            server.closing.clear()
            server.polled.clear()
            if not server.polled.wait(min(_CLOSING_GRACE, deadline - time.monotonic())):
                return "closed"
