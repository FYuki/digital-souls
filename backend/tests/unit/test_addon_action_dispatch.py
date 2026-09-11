"""Gate送信境界を通し、外部副作用とruntime再開を区別する。"""

import asyncio
from dataclasses import replace

import pytest

from app.addon_action.dispatch import ActionDispatch
from app.addon_action.journal import ActionJournal, ActionOutcome
from app.addon_action.models import ApprovalChoice
from app.addon_action.store import ActionStore
from app.external_mcp import Connection, ExecutionContext, ExecutionGate, Registry
from app.external_mcp.models import MCPFailure
from tests.external_mcp_test_support import FakeSource, manifest
from tests.addon_action_test_support import policy


def runtime(tmp_path, *, trusted=False):
    p = policy(tmp_path)
    c = Connection.from_manifest(manifest(trusted=trusted))
    registry = Registry()
    registry.register(c)
    actions = ActionDispatch(ActionJournal(p.store), p.sanitizer)
    gate = ExecutionGate(registry, confirmations=p, actions=actions)
    return c, gate, p, actions.journal


async def approve_once(gate, p, c, loop):
    confirmation = await gate.invoke(c.id, "native-tool", {"value": 1}, loop)
    p.store.answer(confirmation["confirmation_id"], ApprovalChoice.ONCE)
    return await gate.resume_confirmation(confirmation["confirmation_id"], loop)


def test_success_replay_across_process_restart_neither_dispatches_nor_asks_again(
    tmp_path,
):
    async def run():
        c, gate, p, journal = runtime(tmp_path)
        source = FakeSource(c)
        source.result["structuredContent"] = {"password": "secret-never-store"}
        context = ExecutionContext("miori", "session", action_scope="activity")
        async with gate.attach(c.id, source):
            first = await approve_once(gate, p, c, gate.begin_loop(context))
        assert first["outcome"] == "succeeded" and len(source.calls) == 1
        c, restored, p, journal = runtime(tmp_path)
        journal.detach_dispatches()
        async with restored.attach(c.id, source):
            result = await restored.invoke(
                c.id, "native-tool", {"value": 1}, restored.begin_loop(context)
            )
        assert result["execution_id"] == first["execution_id"]
        assert result["outcome"] == "succeeded" and result["replayed"]
        assert len(source.calls) == 1 and len(p.store.requests()) == 1
        assert "secret-never-store" not in journal.store.path.read_bytes().decode(
            errors="ignore"
        )
        assert p.sanitizer.result(result)["replayed"]

    asyncio.run(run())


@pytest.mark.parametrize("started", [False, True, None])
def test_predispatch_failure_is_known_but_lost_response_is_unknown(tmp_path, started):
    async def run():
        c, gate, p, journal = runtime(tmp_path)
        source = FakeSource(c)
        source.failures = [MCPFailure("transport", "lost", request_started=started)]
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(ExecutionContext("miori", "session"))
            result = await approve_once(gate, p, c, loop)
        expected = "failed" if started is False else "result_unknown"
        assert result["outcome"] == expected
        assert p.sanitizer.result(result)["outcome"] == expected
        assert journal.get(result["execution_id"]).outcome.value == expected
        assert len(source.calls) == 1

    asyncio.run(run())


def test_cancel_after_side_effect_and_restart_do_not_replay_or_modify_arguments(
    tmp_path,
):
    async def run():
        c, gate, p, journal = runtime(tmp_path)
        sent = asyncio.Event()

        class SlowSource(FakeSource):
            effects = 0

            async def call_tool(self, *args, **kwargs):
                self.effects += 1
                sent.set()
                await asyncio.Future()

        source = SlowSource(c)
        context = ExecutionContext("miori", "session", action_scope="activity")
        async with gate.attach(c.id, source):
            task = asyncio.create_task(
                approve_once(gate, p, c, gate.begin_loop(context))
            )
            await sent.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        restored = ActionJournal(ActionStore(journal.store.path))
        assert restored.pending()[0].outcome == ActionOutcome.RESULT_UNKNOWN
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(context)
            same = await gate.invoke(c.id, "native-tool", {"value": 1}, loop)
            changed = await gate.invoke(c.id, "native-tool", {"value": 2}, loop)
            assert same["outcome"] == changed["outcome"] == "result_unknown"
            assert same["replayed"] and source.effects == 1
            assert changed.get("replayed", False) is False
            assert len(p.store.requests()) == 1
            # 新しい明示的要求のみ、別scopeとして承認を求められる。
            new_loop = gate.begin_loop(
                replace(context, action_scope="explicit-new-request")
            )
            new = await gate.invoke(c.id, "native-tool", {"value": 2}, new_loop)
            assert new["outcome"] == "confirmation_required"

    asyncio.run(run())


def test_trusted_reads_are_fresh_and_tool_error_does_not_prove_no_side_effect(tmp_path):
    async def run():
        c, gate, p, journal = runtime(tmp_path, trusted=True)
        source = FakeSource(c)
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(ExecutionContext("miori", "session"))
            for _ in range(2):
                assert (await gate.invoke(c.id, "native-tool", {"value": 1}, loop))[
                    "outcome"
                ] == "succeeded"
        assert len(source.calls) == 2 and not journal.pending()
        c, gate, p, journal = runtime(tmp_path)
        source = FakeSource(c)
        source.result = {
            "isError": True,
            "content": [{"type": "text", "text": "partially applied"}],
        }
        async with gate.attach(c.id, source):
            result = await approve_once(
                gate, p, c, gate.begin_loop(ExecutionContext("miori", "session"))
            )
        assert result["outcome"] == "result_unknown"
        assert journal.pending()[0].outcome == ActionOutcome.RESULT_UNKNOWN

    asyncio.run(run())
