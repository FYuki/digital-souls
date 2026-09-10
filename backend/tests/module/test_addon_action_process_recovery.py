"""制御MCPの実stdioとプロセス停止による回復契約受入。

LLM・semantic privacyはこのsuiteの対象外。独立MCP/実会話の証跡とは分ける。
"""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from app.addon_action.dispatch import ActionDispatch
from app.addon_action.journal import ActionJournal
from app.addon_action.models import ApprovalChoice, ExecutionScene
from app.addon_action.recovery import ActionRecovery
from app.external_mcp import (
    Connection,
    ExecutionContext,
    ExecutionGate,
    ExternalMCPClient,
    Registry,
)
from app.external_mcp.models import digest
from tests.external_mcp_test_support import manifest
from tests.unit.test_addon_action_queue import policy


SERVER = (
    Path(__file__).resolve().parents[1]
    / "fixtures/external_mcp/action_recovery_server.py"
)
CONTEXT = ExecutionContext(
    "miori", "process-acceptance", action_scope="durable-activity"
)


async def configuration(root, *, recoverable=True):
    value = manifest()
    value["connection"]["stdio"] = {
        "command": sys.executable,
        "args": [str(SERVER), "--root", str(root / "external")],
    }
    client = ExternalMCPClient(Connection.from_manifest(value), timeout=3)
    async with client.connect():
        tools = (await client.discover()).tools
    if recoverable:
        definitions = {t["name"]: t for t in tools}
        value["core_policy"]["recovery_profiles"] = [
            {
                "contract": "request-key-v1",
                "tool_name": "change",
                "definition_digest": digest(definitions["change"]),
                "request_key_argument": "request_id",
                **{
                    name: {
                        "tool_name": name,
                        "definition_digest": digest(definitions[name]),
                    }
                    for name in ("status", "replay", "cancel")
                },
            }
        ]
    return value


@asynccontextmanager
async def runtime(root, config):
    connection = Connection.from_manifest(config)
    p = policy(root / "core")
    registry = Registry()
    registry.register(connection)
    actions = ActionDispatch(ActionJournal(p.store), p.sanitizer)
    actions.journal.detach_dispatches()
    gate = ExecutionGate(registry, confirmations=p, actions=actions)
    recovery = ActionRecovery(gate, actions)
    gate.recovery = gate.tasks = recovery
    client = ExternalMCPClient(connection, timeout=3)
    async with client.connect(), gate.attach(connection.id, client):
        yield connection, gate, p, recovery


async def invoke(connection, gate, p, mode, *, value=7):
    loop = gate.begin_loop(CONTEXT)
    result = await gate.invoke(
        connection.id, "change", {"mode": mode, "value": value}, loop
    )
    if result["outcome"] == "confirmation_required":
        p.store.answer(result["confirmation_id"], ApprovalChoice.ONCE)
        result = await gate.resume_confirmation(result["confirmation_id"], loop)
    return result, loop


def external_state(root):
    with sqlite3.connect(root / "external/external.sqlite3") as db:
        return {
            "domain": db.execute("SELECT value, effects FROM domain").fetchone(),
            "calls": [row[0] for row in db.execute("SELECT operation FROM calls")],
        }


@pytest.mark.parametrize(
    "mode,expected,effects",
    [
        ("apply", "succeeded", 1),
        ("no_change", "no_change", 0),
        ("conflict", "conflict", 0),
        ("failed", "failed", 0),
    ],
)
def test_external_result_and_repeated_runtime_do_not_dispatch_again(
    tmp_path, mode, expected, effects
):
    async def run():
        config = await configuration(tmp_path)
        async with runtime(tmp_path, config) as (c, gate, p, recovery):
            first, _ = await invoke(c, gate, p, mode)
            assert first["outcome"] == expected, first
            if mode == "conflict":
                assert (
                    p.sanitizer.result(first)["structured"]["latest_state"]["version"]
                    == 0
                )
        async with runtime(tmp_path, config) as (c, gate, p, recovery):
            second, _ = await invoke(c, gate, p, mode)
            assert second["execution_id"] == first["execution_id"]
            assert second["replayed"] and second["outcome"] == expected
        assert external_state(tmp_path)["domain"][1] == effects
        assert external_state(tmp_path)["calls"] == ["change"]

    asyncio.run(run())


@pytest.mark.parametrize("recoverable", [True, False])
def test_external_process_dies_after_commit_and_restart_never_resends(
    tmp_path, recoverable
):
    async def run():
        config = await configuration(tmp_path, recoverable=recoverable)
        async with runtime(tmp_path, config) as (c, gate, p, recovery):
            first, _ = await invoke(c, gate, p, "disconnect")
            assert first["outcome"] == "result_unknown", first
        assert external_state(tmp_path)["domain"] == (7, 1)
        async with runtime(tmp_path, config) as (c, gate, p, recovery):
            result = await recovery.recover(first["execution_id"])
            assert result == ("applied" if recoverable else "result_unknown")
            second, _ = await invoke(c, gate, p, "disconnect")
            assert second["outcome"] == (
                "succeeded" if recoverable else "result_unknown"
            )
            assert second["replayed"]
        calls = external_state(tmp_path)["calls"]
        assert calls == (["change", "status"] if recoverable else ["change"])
        assert external_state(tmp_path)["domain"] == (7, 1)

    asyncio.run(run())


def test_cancel_request_requires_external_stop_observation_after_restart(tmp_path):
    async def run():
        config = await configuration(tmp_path)
        async with runtime(tmp_path, config) as (c, gate, p, recovery):
            first, loop = await invoke(c, gate, p, "task")
            assert first["outcome"] == "running", first
            gate.stop(loop)
            assert (
                await recovery.cancel_execution(first["execution_id"])
                == "cancel_requested"
            )
            assert await recovery.recover(first["execution_id"]) == "running"
            blocked = await gate.invoke(
                c.id, "change", {"mode": "apply", "value": 8}, loop
            )
            assert blocked["outcome"] == "cancel_requested"
        (tmp_path / "external/control.json").write_text('{"cancel_completed": true}')
        async with runtime(tmp_path, config) as (c, gate, p, recovery):
            assert await recovery.recover(first["execution_id"]) == "cancelled"
        assert external_state(tmp_path)["calls"] == [
            "change",
            "cancel",
            "status",
            "status",
        ]
        assert external_state(tmp_path)["domain"] == (0, 0)

    asyncio.run(run())


def test_core_process_crash_before_checkpoint_recovers_only_stored_result(tmp_path):
    config = asyncio.run(configuration(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps(config))
    # 子Coreは外部応答受信後、ActionJournalの結果保存直前で終了する。
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(SERVER.parents[3])},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.returncode == 23, child.stderr
    with sqlite3.connect(tmp_path / "core/actions.sqlite3") as db:
        execution_id, outcome = db.execute(
            "SELECT execution_id, outcome FROM action_executions"
        ).fetchone()
    assert outcome == "dispatching"
    assert external_state(tmp_path)["domain"] == (7, 1)
    (tmp_path / "external/control.json").write_text('{"lookup_unknown": true}')

    async def restore():
        async with runtime(tmp_path, config) as (c, gate, p, recovery):
            assert await recovery.recover(execution_id) == "applied"
            result, _ = await invoke(c, gate, p, "apply")
            assert result["outcome"] == "succeeded" and result["replayed"]

    asyncio.run(restore())
    assert external_state(tmp_path)["calls"] == ["change", "status", "replay"]
    assert external_state(tmp_path)["domain"] == (7, 1)


def test_real_sixty_second_wait_keeps_queue_and_late_once_only_dispatches_future(
    tmp_path,
):
    async def run():
        config = await configuration(tmp_path)
        async with runtime(tmp_path, config) as (c, gate, p, recovery):
            assert p.autonomous_wait_seconds == 60
            context = replace(CONTEXT, scene=ExecutionScene.AUTONOMOUS)
            old_loop = gate.begin_loop(context)
            started = time.monotonic()
            result = await gate.invoke(
                c.id, "change", {"mode": "apply", "value": 1}, old_loop
            )
            elapsed = time.monotonic() - started
            assert result["outcome"] == "deferred", result
            assert 60 <= elapsed < 70
            request = p.store.requests()[0]
            assert not request.waiting and request.choice is None
            assert external_state(tmp_path)["calls"] == []
            gate.end_loop(old_loop)
            p.store.answer(request.id, ApprovalChoice.ONCE)
            assert p.store.state(request.key).remaining == 1
            assert external_state(tmp_path)["calls"] == []
            future = gate.begin_loop(replace(context, action_scope="future-activity"))
            result = await gate.invoke(
                c.id, "change", {"mode": "apply", "value": 2}, future
            )
            assert result["outcome"] == "succeeded", result
            assert p.store.state(request.key).remaining == 0
            assert external_state(tmp_path)["domain"] == (2, 1)
            assert external_state(tmp_path)["calls"] == ["change"]
            assert not p.store.request(request.id).waiting

    asyncio.run(run())


if __name__ == "__main__":

    async def crash():
        root = Path(sys.argv[1])
        async with runtime(root, json.loads((root / "config.json").read_text())) as (
            c,
            gate,
            p,
            recovery,
        ):

            def before_checkpoint(*args, **kwargs):
                os._exit(23)

            recovery.actions.finish = before_checkpoint
            await invoke(c, gate, p, "apply")

    asyncio.run(crash())
