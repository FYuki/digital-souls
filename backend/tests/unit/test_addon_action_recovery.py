"""接続先の保証付き状態照会・結果再返却・cancelを実行記録と照合する。"""

import asyncio
import sqlite3
from dataclasses import replace
from copy import deepcopy

from app.addon_action.dispatch import ActionDispatch
from app.addon_action.journal import ActionJournal, ActionOutcome
from app.addon_action.recovery import ActionRecovery
from app.external_mcp import Connection, ExecutionContext, ExecutionGate, Registry
from app.external_mcp.models import digest, Discovery
from tests.external_mcp_test_support import FakeSource, manifest, discovery
from tests.unit.test_addon_action_dispatch import approve_once
from tests.addon_action_test_support import policy


def server_contract():
    original = deepcopy(discovery().tools[0])
    original["inputSchema"]["properties"]["request_id"] = {"type": "string"}
    tools = [original] + [
        {
            "name": name,
            "inputSchema": {
                "type": "object",
                "properties": {"request_id": {"type": "string"}},
                "required": ["request_id"],
                "additionalProperties": False,
            },
        }
        for name in ("action_status", "action_replay", "action_cancel")
    ]
    data = Discovery("2026-07-28", tuple(tools))
    config = manifest(trusted=True)
    profile = {
        "contract": "request-key-v1",
        "tool_name": "native-tool",
        "definition_digest": digest(original),
        "request_key_argument": "request_id",
        **{
            key: {"tool_name": tool["name"], "definition_digest": digest(tool)}
            for key, tool in zip(("status", "replay", "cancel"), tools[1:])
        },
    }
    config["core_policy"]["recovery_profiles"] = [profile]
    return Connection.from_manifest(config), data


class ExternalTasks(FakeSource):
    def __init__(self, connection, data):
        super().__init__(connection, data)
        self.effects = 0
        self.state = "running"
        self.lookup = None
        self.key = None

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments, kwargs))
        if name == "native-tool":
            self.effects += 1
            self.key = arguments["request_id"]
        else:
            assert arguments == {"request_id": self.key}
        state = self.state
        if name == "action_status" and self.lookup is not None:
            state = self.lookup
        if name == "action_cancel":
            state = "cancel_requested"
        return {
            "structuredContent": {
                "action": {
                    "status": state,
                    "task_id": "external-task",
                    "result": {"count": self.effects},
                    "latest_state": {"version": 2, "password": "do-not-persist"},
                }
            }
        }


def runtime(tmp_path, c):
    p = policy(tmp_path)
    registry = Registry()
    registry.register(c)
    actions = ActionDispatch(ActionJournal(p.store), p.sanitizer)
    gate = ExecutionGate(registry, confirmations=p, actions=actions)
    recovery = ActionRecovery(gate, actions)
    gate.recovery = gate.tasks = recovery
    actions.on_change = recovery.changed
    return gate, p, recovery


def test_task_survives_restart_and_cancel_request_is_not_external_stop(tmp_path):
    async def run():
        c, data = server_contract()
        source = ExternalTasks(c, data)
        gate, p, recovery = runtime(tmp_path, c)
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(ExecutionContext("miori", "session"))
            first = await approve_once(gate, p, c, loop)
            assert first["outcome"] == "running"
            assert "request_id" not in p.store.requests()[0].preview["arguments"]
            assert (
                source.key
                == recovery.actions.journal.get(first["execution_id"]).request_key
            )
            assert not recovery.actions.journal.get(
                first["execution_id"]
            ).cancel_requested
            gate.end_loop(loop)
            # 通常の会話loop終了で外部Taskをキャンセルしない。
            assert not recovery.actions.journal.get(
                first["execution_id"]
            ).cancel_requested
        gate, p, restored = runtime(tmp_path, c)
        restored.actions.journal.detach_dispatches()
        async with gate.attach(c.id, source):
            assert await restored.status(c.id, "external-task") == "running"
            assert await restored.cancel(c.id, "external-task") == "cancel_requested"
            assert restored.actions.journal.get(first["execution_id"]).cancel_requested
            assert source.state == "running"
            source.state = "cancelled"
            assert await restored.status(c.id, "external-task") == "cancelled"
        assert source.effects == 1
        assert "do-not-persist" not in p.store.path.read_bytes().decode(errors="ignore")

    asyncio.run(run())


def test_unknown_uses_stored_result_replay_and_stop_blocks_new_actions(tmp_path):
    async def run():
        c, data = server_contract()
        source = ExternalTasks(c, data)
        source.state = "result_unknown"
        gate, p, recovery = runtime(tmp_path, c)
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(ExecutionContext("miori", "session"))
            first = await approve_once(gate, p, c, loop)
            assert first["outcome"] == "result_unknown"
            gate.stop(loop)
            blocked = await gate.invoke(c.id, "native-tool", {"value": 2}, loop)
            assert blocked["outcome"] == "cancel_requested"
            source.state, source.lookup = "applied", "result_unknown"
            assert await recovery.recover(first["execution_id"]) == "applied"
            assert source.effects == 1
            assert [c[0] for c in source.calls] == [
                "native-tool",
                "action_status",
                "action_replay",
            ]
            record = recovery.actions.journal.get(first["execution_id"])
            assert record.outcome == ActionOutcome.APPLIED and record.cancel_requested

    asyncio.run(run())


def test_changed_definition_and_disabled_connection_cannot_use_recovery_adapter(
    tmp_path,
):
    async def run():
        c, data = server_contract()
        source = ExternalTasks(c, data)
        gate, p, recovery = runtime(tmp_path, c)
        async with gate.attach(c.id, source):
            first = await approve_once(
                gate, p, c, gate.begin_loop(ExecutionContext("miori", "session"))
            )
            changed = deepcopy(data.tools)
            changed[1]["description"] = "different contract"
            source.data = replace(data, tools=changed)
            await gate.refresh(c.id)
            assert await recovery.recover(first["execution_id"]) == "running"
            assert len(source.calls) == 1
            source.data = data
            await gate.refresh(c.id)
            gate.registry.set_enabled(c.id, False)
            assert await recovery.recover(first["execution_id"]) == "running"
            assert len(source.calls) == 1

    asyncio.run(run())


def test_external_conflict_carries_latest_state_and_requires_core_redecision(tmp_path):
    async def run():
        c, data = server_contract()
        source = ExternalTasks(c, data)
        source.state = "conflict"
        gate, p, recovery = runtime(tmp_path, c)
        async with gate.attach(c.id, source):
            result = await approve_once(
                gate, p, c, gate.begin_loop(ExecutionContext("miori", "session"))
            )
            assert result["outcome"] == "conflict"
            projected = p.sanitizer.result(result)
            assert projected["structured"]["latest_state"]["version"] == 2
            assert projected["structured"]["latest_state"]["password"] == "[非公開]"
            assert await recovery.recover(result["execution_id"]) == "conflict"
            assert source.effects == 1

    asyncio.run(run())


def test_startup_worker_observes_external_completion_without_new_write(tmp_path):
    async def run():
        c, data = server_contract()
        source = ExternalTasks(c, data)
        gate, p, recovery = runtime(tmp_path, c)
        async with gate.attach(c.id, source):
            first = await approve_once(
                gate, p, c, gate.begin_loop(ExecutionContext("miori", "session"))
            )
        gate, p, restored = runtime(tmp_path, c)
        source.state = "applied"
        async with gate.attach(c.id, source):
            restored.start()
            try:
                async with asyncio.timeout(2):
                    while (
                        restored.actions.journal.get(first["execution_id"]).outcome
                        != ActionOutcome.APPLIED
                    ):
                        await asyncio.sleep(0.005)
            finally:
                await restored.close()
            assert not gate._loops and restored._task is None
        assert source.effects == 1

    asyncio.run(run())


def test_polling_storage_error_does_not_permanently_stop_recovery(tmp_path, caplog):
    async def run():
        c, data = server_contract()
        source = ExternalTasks(c, data)
        gate, p, recovery = runtime(tmp_path, c)
        async with gate.attach(c.id, source):
            first = await approve_once(gate, p, c, gate.begin_loop(ExecutionContext("miori", "session")))
            original = recovery.actions.journal.pending
            failed = asyncio.Event()
            def pending():
                if not failed.is_set():
                    failed.set()
                    raise sqlite3.OperationalError("private-payload-must-not-be-logged")
                return original()
            recovery.actions.journal.pending = pending
            recovery.start()
            try:
                await asyncio.wait_for(failed.wait(), 1)
                assert not recovery._task.done()
                source.state = "applied"
                recovery._wake.set()
                async with asyncio.timeout(2):
                    while recovery.actions.journal.get(first["execution_id"]).outcome != ActionOutcome.APPLIED:
                        await asyncio.sleep(0.01)
            finally:
                await recovery.close()
        assert source.effects == 1
    asyncio.run(run())
    assert "OperationalError" in caplog.text and "private-payload" not in caplog.text


def test_conflict_survives_failure_of_optional_recovery_lookup(tmp_path):
    from app.external_mcp.models import MCPFailure
    async def run():
        c, data = server_contract()
        source = ExternalTasks(c, data)
        source.state = "conflict"
        gate, p, recovery = runtime(tmp_path, c)
        async def failed(_):
            raise MCPFailure("recovery", "lookup_unavailable")
        recovery.recover = failed
        async with gate.attach(c.id, source):
            result = await approve_once(gate, p, c, gate.begin_loop(ExecutionContext("miori", "session")))
            assert result["outcome"] == "conflict"
            assert recovery.actions.journal.get(result["execution_id"]).outcome == ActionOutcome.CONFLICT
            assert source.effects == 1
    asyncio.run(run())


def test_recovery_revalidates_original_binding_and_privacy_before_query(tmp_path):
    async def run():
        c, data = server_contract()
        source = ExternalTasks(c, data)
        gate, p, recovery = runtime(tmp_path, c)

        class Binding:
            allowed = True

            async def validate(
                self, connection, operation, character, binding, session
            ):
                assert binding == "original-target" and operation == "native-tool"
                return self.allowed

        gate.bindings = Binding()
        async with gate.attach(c.id, source):
            first = await approve_once(
                gate,
                p,
                c,
                gate.begin_loop(
                    ExecutionContext("miori", "session", binding_id="original-target")
                ),
            )
            gate.bindings.allowed = False
            assert await recovery.recover(first["execution_id"]) == "running"
            assert len(source.calls) == 1
            gate.bindings.allowed = True

            async def blocked(arguments):
                return False

            p.egress = blocked
            assert await recovery.recover(first["execution_id"]) == "running"
            assert len(source.calls) == 1

    asyncio.run(run())


def test_earlier_external_completion_wins_over_late_running_response(tmp_path):
    async def run():
        c, data = server_contract()
        source = ExternalTasks(c, data)
        gate, p, recovery = runtime(tmp_path, c)
        async with gate.attach(c.id, source):
            first = await approve_once(
                gate, p, c, gate.begin_loop(ExecutionContext("miori", "session"))
            )
            source.state = "applied"
            assert await recovery.recover(first["execution_id"]) == "applied"
            late = {
                "outcome": "succeeded",
                "native_payload": {
                    "structuredContent": {"action": {"status": "running"}}
                },
            }
            recovery.actions.finish(first["execution_id"], late)
            assert late["outcome"] == "succeeded"
            assert late["result_projection"]["structured"]["result"]["count"] == 1

    asyncio.run(run())


def test_conflict_without_latest_state_fetches_external_truth_before_return(tmp_path):
    async def run():
        c, data = server_contract()

        class LateState(ExternalTasks):
            async def call_tool(self, name, arguments, **kwargs):
                response = await super().call_tool(name, arguments, **kwargs)
                if name == "native-tool":
                    response["structuredContent"]["action"].pop("latest_state")
                return response

        source = LateState(c, data)
        source.state = "conflict"
        gate, p, recovery = runtime(tmp_path, c)
        async with gate.attach(c.id, source):
            result = await approve_once(
                gate, p, c, gate.begin_loop(ExecutionContext("miori", "session"))
            )
            assert result["outcome"] == "conflict"
            assert (
                result["result_projection"]["structured"]["latest_state"]["version"]
                == 2
            )
            assert [call[0] for call in source.calls] == [
                "native-tool",
                "action_status",
            ]

    asyncio.run(run())
