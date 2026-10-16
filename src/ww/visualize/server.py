# SPDX-License-Identifier: GPL-3.0-or-later
"""The local server behind ``ww visualize``.

It listens on 127.0.0.1 only and answers reads: the page, the Workflows and
Tasks data as JSON, one artifact's text, and an event stream that says when
a watched file changed, so the open page refetches only what it shows. Data
is built per request through the core, and kept until the next change of its
kind. It runs until the operator stops it.
"""

from __future__ import annotations

import errno
import hashlib
import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ww.errors import StateError, WwError
from ww.storage import Storage

from .atlas import build_atlas
from .project import Project
from .tasks import artifact_text, task_detail, task_list
from .watch import Watcher

# The lowest and the number of ports a project's page is hashed into; the
# operator page uses 40000 and up.
_PORT_BASE = 30000
_PORT_SPAN = 10000
# How often the event stream sends a comment, so a closed tab is noticed and
# proxies keep the connection.
_HEARTBEAT = 15.0
# The host names a request to this server may name; any other is refused.
_LOOPBACK_NAMES = ("127.0.0.1", "localhost")


def visualizer_port(storage: Storage) -> int:
    """A port fixed per project, so a bookmarked page finds it again."""
    digest = hashlib.sha256(str(storage.root.resolve()).encode("utf-8")).digest()
    return _PORT_BASE + int.from_bytes(digest[:4], "big") % _PORT_SPAN


def visualizer_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def page_html() -> str:
    return files("ww.visualize").joinpath("page.html").read_text("utf-8")


class VisualizerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, storage: Storage, watcher: Watcher) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.storage = storage
        self.watcher = watcher
        self.html = page_html()
        self._cache: dict[tuple[str, int], Any] = {}
        self._lock = threading.Lock()

    def project(self) -> Project:
        return Project.load(self.storage)

    def cached(self, kind: str, version: int, build: Callable[[], Any]) -> Any:
        """``build``'s result, reused until the watched files of ``kind`` change."""
        with self._lock:
            key = (kind, version)
            if key not in self._cache:
                self._cache = {
                    entry: value
                    for entry, value in self._cache.items()
                    if entry[0] != kind
                }
                self._cache[key] = build()
            return self._cache[key]


class _Handler(BaseHTTPRequestHandler):
    server: VisualizerServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """The page is served to a person; its request log has no reader."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        if not self._addressed_here():
            self._send(403, b"forbidden", "text/plain")
            return
        url = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        watcher = self.server.watcher
        try:
            if url.path == "/":
                self._send(200, self.server.html.encode("utf-8"), "text/html")
            elif url.path == "/api/events":
                self._events()
            elif url.path == "/api/atlas":
                self._json(
                    {
                        **self.server.cached(
                            "atlas",
                            watcher.config,
                            lambda: build_atlas(self.server.project()),
                        ),
                        "root": str(self.server.storage.root),
                        "versions": [watcher.config, watcher.tasks],
                    }
                )
            elif url.path == "/api/tasks":
                self._json(
                    {
                        **self.server.cached(
                            "tasks",
                            watcher.tasks,
                            lambda: task_list(self.server.project()),
                        ),
                        "versions": [watcher.config, watcher.tasks],
                    }
                )
            elif url.path == "/api/task":
                self._json(task_detail(self.server.project(), _required(query, "id")))
            elif url.path == "/api/artifact":
                text = artifact_text(
                    self.server.project(),
                    _required(query, "task"),
                    _required(query, "run"),
                    _required(query, "ref"),
                )
                self._send(200, text.encode("utf-8"), "text/plain")
            else:
                self._send(404, b"not found", "text/plain")
        except (StateError, ValueError) as error:
            self._json({"error": str(error)}, 400)
        except WwError as error:
            self._json({"error": str(error)}, 500)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _addressed_here(self) -> bool:
        """Whether the request names this server as the host it was sent to.

        Binding to 127.0.0.1 keeps other machines out, and the missing CORS
        headers keep other sites from reading a response, but a site whose
        domain is re-pointed at 127.0.0.1 (DNS rebinding) is then same-origin
        with the page. Its requests still carry that domain in ``Host``, so
        only a loopback name on this server's port is answered.
        """
        port = self.server.server_address[1]
        allowed = {f"{name}:{port}" for name in _LOOPBACK_NAMES}
        return (self.headers.get("Host") or "").lower() in allowed

    def _events(self) -> None:
        """Say ``change`` with what changed, each time a watched file does."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        watcher = self.server.watcher
        seen = (watcher.config, watcher.tasks)
        self._write(f"event: hello\ndata: {json.dumps(list(seen))}\n\n")
        while not watcher.stopped:
            now = watcher.wait(seen, _HEARTBEAT)
            if now == seen:
                self._write(": still here\n\n")
                continue
            change = {"config": now[0] != seen[0], "tasks": now[1] != seen[1]}
            seen = now
            self._write(f"event: change\ndata: {json.dumps(change)}\n\n")

    def _write(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    def _json(self, body: object, status: int = 200) -> None:
        self._send(status, json.dumps(body).encode("utf-8"), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def _required(query: dict[str, str], name: str) -> str:
    value = query.get(name)
    if not value:
        raise ValueError(f"the request needs {name!r}")
    return value


def bind_visualizer(storage: Storage, port: int, watcher: Watcher) -> VisualizerServer:
    try:
        return VisualizerServer(port, storage, watcher)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            raise StateError(
                f"port {port} is in use; a page for this project may already be "
                f"open at {visualizer_url(port)}, or pass --port with a free port"
            ) from error
        raise StateError(
            f"ww visualize cannot listen on port {port}: {error}"
        ) from error


def serve_visualizer(
    storage: Storage,
    *,
    port: int | None = None,
    open_browser: Callable[[str], object] | None = None,
    ready: Callable[[str], None] | None = None,
    interval: float = 1.0,
) -> None:
    """Serve the page until interrupted; ``ready`` hears the URL once it listens."""
    watcher = Watcher(lambda: Project.load(storage), interval=interval)
    server = bind_visualizer(
        storage, visualizer_port(storage) if port is None else port, watcher
    )
    url = visualizer_url(server.server_address[1])
    watcher.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if ready is not None:
            ready(url)
        if open_browser is not None:
            open_browser(url)
        thread.join()
    finally:
        watcher.stop()
        server.shutdown()
        server.server_close()
