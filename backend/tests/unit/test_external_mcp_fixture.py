"""通信分割に対する合成MCP middlewareの回帰検証。"""

import asyncio
import runpy
import sys
from pathlib import Path


def test_fault_middleware_replays_all_asgi_body_chunks(tmp_path, monkeypatch):
    state = tmp_path / "count"
    state.write_text("1")
    monkeypatch.setattr(sys, "argv", ["server.py", "--fault-state", str(state)])
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "fixtures/external_mcp/server.py")
    )
    messages = [
        {"type": "http.request", "body": b'{"method":"tools/', "more_body": True},
        {"type": "http.request", "body": b'call"}', "more_body": False},
    ]
    received = []

    async def run():
        queue = iter(messages)

        async def receive():
            return next(queue)

        async def send(message):
            pass

        async def downstream(scope, receive, send):
            received.extend([await receive(), await receive()])

        await namespace["AuthFixture"](downstream)(
            {"type": "http", "method": "POST", "headers": []}, receive, send
        )

    asyncio.run(run())
    assert received == messages
    assert state.read_text() == "2"
