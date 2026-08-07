from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socket import AF_INET6
from threading import Thread
from typing import Mapping, cast


READY_GATE_PATH = "/ready"


class _ReadyServer(ThreadingHTTPServer):
    ready_path: str


class _Ipv6ReadyServer(_ReadyServer):
    address_family = AF_INET6


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
    def __init__(self, endpoint: Mapping[str, object]) -> None:
        host = endpoint.get("host")
        port = endpoint.get("port")
        if not isinstance(host, str) or not host:
            raise ValueError("environment ready gate host is required")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("environment ready gate port is required")
        self._address = (host, port)
        self._path = READY_GATE_PATH
        self._server: _ReadyServer | None = None
        self._thread: Thread | None = None

    def open(self) -> None:
        server_type = _Ipv6ReadyServer if ":" in self._address[0] else _ReadyServer
        server = server_type(self._address, _ReadyHandler)
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
