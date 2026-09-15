"""Event適合試験のprocess所有・仮想時刻・標準MCP接続。"""
import asyncio
import json
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from app.addon_events.contracts import Limits, Operation, Source
from app.addon_events.runtime import EventRuntime
from app.external_mcp import Connection, ExecutionGate, ExternalMCPClient, Registry
from app.external_mcp.models import digest
from app.tool_use.binding import BindingResolver
from app.tool_use.projection import Sanitizer
from tests.external_mcp_test_support import manifest
from tests.tool_use_test_support import Scanner

SERVER = Path(__file__).parent / "fixtures/addon_events/server.py"


class Clock:
    def __init__(self):
        self.value = 1_800_000_000.
    def __call__(self):
        return self.value
    def advance(self, seconds=60):
        self.value += seconds


class Control:
    def __init__(self, root):
        self.root = root
        self.value = {"epoch": "era1", "position": 10}
        self.update()
    def update(self, **kwargs):
        self.value.update(kwargs)
        temporary = self.root / "next.json"
        temporary.write_text(json.dumps(self.value))
        temporary.replace(self.root / "state.json")
    def calls(self, method=None):
        path = self.root / "calls.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return sum(row["method"] == method for row in rows) if method else rows


@asynccontextmanager
async def connected(tmp_path, transport="stdio", *, trusted=True, bindings=(), binding=False, sharing=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    control = Control(tmp_path)
    args = [str(SERVER), "--state", str(tmp_path / "state.json"), "--calls", str(tmp_path / "calls.jsonl"),
            "--pid", str(tmp_path / "pid")]
    process = None
    if transport == "streamable_http":
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, *args, "--port", str(port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            if process.poll() is not None:
                raise AssertionError("fixture exited")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.1):
                    break
            except OSError:
                await asyncio.sleep(.03)
        else:
            process.kill()
            process.wait()
            raise AssertionError("fixture startup timeout")
        value = manifest(trusted=trusted, transport=transport, endpoint=f"http://127.0.0.1:{port}/mcp",
                         binding=binding, sharing=sharing)
    else:
        value = manifest(trusted=trusted, binding=binding, sharing=sharing)
        value["connection"]["stdio"] = {"command": sys.executable, "args": args}
    connection = Connection.from_manifest(value)
    registry = Registry()
    registry.register(connection)
    gate = ExecutionGate(registry, bindings=BindingResolver(bindings))
    client = ExternalMCPClient(connection, timeout=2)
    try:
        async with client.connect(), gate.attach(connection.id, client):
            yield control, gate, client
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def profile(gate, *, kind="tool", limits=None, wake=False, binding_id=None, arguments="{}"):
    entry = gate.registry.entry("external-test")
    snapshot = entry.staged or entry.active
    def operation(name):
        definitions = snapshot.document["tools" if kind == "tool" else "resources"]
        ref = name if kind == "tool" else f"events://fixture/{name}"
        item = next(d for d in definitions if d["name" if kind == "tool" else "uri"] == ref)
        return Operation(kind, ref, digest(item["native_definition"] if kind == "tool" else item))
    return Source("fixture", "external-test", "miori", operation("history"), operation("snapshot"),
                  binding_id=binding_id, arguments_json=arguments,
                  wake_resource="events://fixture/wake" if wake else None, limits=limits or Limits())


def runtime(gate, source, tmp_path, clock=None, sanitizer=None):
    clock = clock or Clock()
    return EventRuntime(gate, (source,), tmp_path / "events.sqlite3",
                        sanitizer or Sanitizer(Scanner(), ("synthetic-secret",)),
                        clock=clock, monotonic=clock)


async def until(predicate, timeout=5):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= end:
            raise AssertionError("condition timeout")
        await asyncio.sleep(.02)
