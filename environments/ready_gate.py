from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import cast
from urllib.parse import urlsplit


class _ReadyServer(ThreadingHTTPServer):
    ready_path: str


class _ReadyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != cast(_ReadyServer, self.server).ready_path:
            self.send_error(404)
            return
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *args: object) -> None:
        return


class ReadyGate:
    def __init__(self, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.port is None
            or not parsed.path.startswith("/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("environment ready gate must be a local HTTP URL")
        self._address = (parsed.hostname, parsed.port)
        self._path = parsed.path
        self._server: _ReadyServer | None = None
        self._thread: Thread | None = None

    def open(self) -> None:
        server = _ReadyServer(self._address, _ReadyHandler)
        server.ready_path = self._path
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread

    def close(self) -> None:
        if self._server is None or self._thread is None:
            raise RuntimeError("environment ready gate is not open")
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        self._server = None
        self._thread = None
