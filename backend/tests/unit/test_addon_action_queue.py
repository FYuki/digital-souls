"""#301 確認キュー・待機終了・遅い承認・単回許可・dispatch競合。"""

import asyncio
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.addon_action.models import (
    ActionInvocation,
    ApprovalChoice,
    ExecutionScene,
    Permission,
)
from app.addon_action.store import ActionStore
from app.external_mcp import Connection, ExecutionContext, ExecutionGate, Registry
from app.external_mcp.models import ConfirmationNeeded, MCPFailure
from tests.external_mcp_test_support import manifest, FakeSource






from tests.addon_action_test_support import policy

def invocation(**changes):
    return replace(
        ActionInvocation(
            "external-test",
            "identity",
            "テスト接続",
            "miori",
            "conversation",
            "loop",
            ExecutionScene.CONVERSATION,
            "write",
            {"effect": "unknown", "source": "unknown"},
            {"value": 1},
        ),
        **changes,
    )




def test_queue_keeps_request_after_timeout_and_late_once_only_affects_future(tmp_path):
    async def run():
        clock = [100.0]
        p = policy(tmp_path, clock=lambda: clock[0])
        with pytest.raises(ConfirmationNeeded) as needed:
            await p.prepare(invocation(), live=lambda: None)
        request_id = needed.value.request_id
        p.end_loop("loop")
        saved = p.store.request(request_id)
        assert not saved.waiting and saved.choice is None
        p.store.answer(request_id, ApprovalChoice.ONCE)
        with pytest.raises(MCPFailure, match="confirmation_wait_ended"):
            await p.prepare(invocation(), live=lambda: None, request_id=request_id)
        future = await p.prepare(invocation(loop_id="future"), live=lambda: None)
        assert p.consume(future)
        assert not p.consume(future)
        assert (
            ActionStore(p.store.path).request(request_id).choice == ApprovalChoice.ONCE
        )

    asyncio.run(run())


def test_confirmation_deduplication_keeps_identity_and_scene_separate(tmp_path):
    async def run():
        p = policy(tmp_path, autonomous_wait_seconds=0.01)
        for call in (invocation(), invocation(connection_identity="new-identity")):
            with pytest.raises(ConfirmationNeeded):
                await p.prepare(call, live=lambda: None)
        with pytest.raises(MCPFailure, match="confirmation_wait_ended"):
            await p.prepare(invocation(scene=ExecutionScene.AUTONOMOUS), live=lambda: None)
        requests = p.store.requests()
        assert len(requests) == 3
        assert len({r.fingerprint for r in requests}) == 3
        assert len({r.key for r in requests}) == 3
    asyncio.run(run())


def test_invalidating_one_connection_preserves_other_confirmation_in_same_loop(tmp_path):
    async def run():
        p = policy(tmp_path)
        a = Connection.from_manifest(manifest(connection_id="a"))
        b = Connection.from_manifest(manifest(connection_id="b"))
        registry = Registry()
        registry.register(a)
        registry.register(b)
        gate = ExecutionGate(registry, confirmations=p)
        source_a, source_b = FakeSource(a), FakeSource(b)
        async with gate.attach(a.id, source_a), gate.attach(b.id, source_b):
            loop = gate.begin_loop(ExecutionContext("miori", "session"))
            first = await gate.invoke(a.id, "native-tool", {"value": 1}, loop)
            second = await gate.invoke(b.id, "native-tool", {"value": 1}, loop)
            gate.invalidate_connection(a.id)
            assert not p.store.request(first["confirmation_id"]).waiting
            assert p.store.request(second["confirmation_id"]).waiting
            p.store.answer(second["confirmation_id"], ApprovalChoice.ONCE)
            result = await gate.resume_confirmation(second["confirmation_id"], loop)
            assert result["outcome"] == "succeeded"
            assert not source_a.calls and len(source_b.calls) == 1
    asyncio.run(run())


def test_repeated_answer_and_ticket_do_not_duplicate_once_or_always_dispatch(tmp_path):
    async def run():
        p = policy(tmp_path)
        with pytest.raises(ConfirmationNeeded) as needed:
            await p.prepare(invocation(), live=lambda: None)
        rid = needed.value.request_id
        stores = [ActionStore(p.store.path) for _ in range(10)]
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda s: s.answer(rid, ApprovalChoice.ONCE), stores))
        assert p.store.state(p.store.request(rid).key).remaining == 1
        ticket = await p.prepare(invocation(), live=lambda: None, request_id=rid)
        assert p.consume(ticket)
        assert not p.consume(ticket)
        with pytest.raises(MCPFailure, match="confirmation_already_answered"):
            p.store.answer(rid, ApprovalChoice.ALWAYS)

    asyncio.run(run())


def test_autonomous_wait_timeout_uses_config_and_does_not_persist_denial(tmp_path):
    async def run():
        p = policy(tmp_path, autonomous_wait_seconds=0.02)
        call = invocation(scene=ExecutionScene.AUTONOMOUS)
        with pytest.raises(MCPFailure, match="confirmation_wait_ended"):
            await p.prepare(call, live=lambda: None)
        saved = p.store.requests()[0]
        assert saved.wait_until - saved.created_at == pytest.approx(0.02)
        assert not saved.waiting and saved.choice is None
        assert p.store.state(saved.key).permission == Permission.UNAPPROVED
        p.store.answer(saved.id, ApprovalChoice.ALWAYS)
        assert p.store.state(saved.key).allowed
        assert not p.store.request(saved.id).waiting

    asyncio.run(run())
    assert policy(tmp_path).autonomous_wait_seconds == 60


def test_autonomous_reject_stays_denied_without_requeue_and_conversation_reasks(
    tmp_path,
):
    async def run():
        p = policy(tmp_path, autonomous_wait_seconds=1)
        task = asyncio.create_task(
            p.prepare(invocation(scene=ExecutionScene.AUTONOMOUS), live=lambda: None)
        )
        await asyncio.sleep(0)
        saved = p.store.requests()[0]
        p.store.answer(saved.id, ApprovalChoice.REJECT)
        with pytest.raises(MCPFailure, match="action_rejected"):
            await task
        with pytest.raises(MCPFailure, match="action_rejected"):
            await p.prepare(
                invocation(scene=ExecutionScene.AUTONOMOUS, loop_id="next"),
                live=lambda: None,
            )
        assert len(p.store.requests()) == 1
        with pytest.raises(ConfirmationNeeded) as needed:
            await p.prepare(invocation(loop_id="dialogue"), live=lambda: None)
        p.store.answer(needed.value.request_id, ApprovalChoice.REJECT)
        with pytest.raises(ConfirmationNeeded):
            await p.prepare(invocation(loop_id="new-dialogue"), live=lambda: None)

    asyncio.run(run())


def test_timeout_beats_late_choice_without_consuming_future_credit(tmp_path):
    async def run():
        clock = [0.0]
        p = policy(tmp_path, clock=lambda: clock[0], autonomous_wait_seconds=60)
        task = asyncio.create_task(
            p.prepare(invocation(scene=ExecutionScene.AUTONOMOUS), live=lambda: None)
        )
        await asyncio.sleep(0)
        saved = p.store.requests()[0]
        clock[0] = 60
        p.store.answer(saved.id, ApprovalChoice.ONCE)
        with pytest.raises(MCPFailure, match="confirmation_wait_ended"):
            await task
        assert p.store.state(saved.key).remaining == 1

    asyncio.run(run())


def test_gate_confirmation_resume_single_dispatch_and_stop(tmp_path):
    async def run():
        p = policy(tmp_path)
        c = Connection.from_manifest(manifest())
        registry = Registry()
        registry.register(c)
        gate = ExecutionGate(registry, confirmations=p)
        source = FakeSource(c)
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(ExecutionContext("miori", "conversation"))
            result = await gate.invoke(c.id, "native-tool", {"value": 1}, loop)
            assert result["outcome"] == "confirmation_required"
            assert not source.calls
            rid = result["confirmation_id"]
            p.store.answer(rid, ApprovalChoice.ALWAYS)
            assert (await gate.resume_confirmation(rid, loop))["outcome"] == "succeeded"
            with pytest.raises(MCPFailure, match="invalid_confirmation_resume"):
                await gate.resume_confirmation(rid, loop)
            assert len(source.calls) == 1
            gate.end_loop(loop)
            # 別場面の要求を止め、後から承認しても旧loopを再開させない。
            new_loop = gate.begin_loop(
                ExecutionContext("miori", "other", scene=ExecutionScene.AUTONOMOUS)
            )
            task = asyncio.create_task(
                gate.invoke(c.id, "native-tool", {"value": 2}, new_loop)
            )
            await asyncio.sleep(0)
            gate.stop(new_loop)
            assert (await task)["outcome"] == "cancel_requested"
            assert len(source.calls) == 1
            gate.end_loop(new_loop)

    asyncio.run(run())


def test_egress_is_rechecked_after_queue_before_any_dispatch(tmp_path):
    async def run():
        p = policy(tmp_path)
        c = Connection.from_manifest(manifest())
        registry = Registry()
        registry.register(c)
        gate = ExecutionGate(registry, confirmations=p)
        source = FakeSource(c)
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(ExecutionContext("miori", "conversation"))
            initial = await gate.invoke(c.id, "native-tool", {"value": 1}, loop)
            p.store.answer(initial["confirmation_id"], ApprovalChoice.ALWAYS)

            async def blocked(arguments):
                return False

            p.egress = blocked
            final = await gate.resume_confirmation(initial["confirmation_id"], loop)
            assert final["native_error"]["code"] == "egress_privacy_blocked"
            assert not source.calls
            gate.end_loop(loop)

    asyncio.run(run())


def test_end_runtime_waiters_keeps_queue_and_permission(tmp_path):
    async def run():
        p = policy(tmp_path)
        with pytest.raises(ConfirmationNeeded) as needed:
            await p.prepare(invocation(), live=lambda: None)
        restored = ActionStore(p.store.path)
        restored.detach_waiters()
        assert not restored.request(needed.value.request_id).waiting
        restored.answer(needed.value.request_id, ApprovalChoice.ONCE)
        assert (
            restored.state(restored.request(needed.value.request_id).key).remaining == 1
        )

    asyncio.run(run())


def test_confirmation_resume_keeps_dispatch_guard(tmp_path):
    async def run():
        p = policy(tmp_path)
        c = Connection.from_manifest(manifest())
        registry = Registry()
        registry.register(c)
        gate = ExecutionGate(registry, confirmations=p)
        source = FakeSource(c)
        valid = [True]

        def guard():
            if not valid[0]:
                raise MCPFailure("policy", "autonomy_revoked")

        async with gate.attach(c.id, source):
            loop = gate.begin_loop(ExecutionContext("miori", "conversation"))
            initial = await gate.invoke(
                c.id, "native-tool", {"value": 1}, loop, dispatch_guard=guard
            )
            p.store.answer(initial["confirmation_id"], ApprovalChoice.ONCE)
            valid[0] = False
            result = await gate.resume_confirmation(initial["confirmation_id"], loop)
            assert result["native_error"]["code"] == "autonomy_revoked"
            assert not source.calls
            assert (
                p.store.state(p.store.request(initial["confirmation_id"]).key).remaining
                == 1
            )
            gate.end_loop(loop)

    asyncio.run(run())


def test_additional_answers_are_bound_to_confirmation_and_checked_for_core_write(
    tmp_path,
):
    async def run():
        p = policy(tmp_path, protected_roots=(tmp_path / "core",))
        call = invocation(input_responses={"answer": {"value": "original"}})
        with pytest.raises(ConfirmationNeeded) as needed:
            await p.prepare(call, live=lambda: None)
        p.store.answer(needed.value.request_id, ApprovalChoice.ALWAYS)
        with pytest.raises(MCPFailure, match="confirmation_mismatch"):
            await p.prepare(
                replace(call, input_responses={"answer": {"value": "changed"}}),
                live=lambda: None,
                request_id=needed.value.request_id,
            )
        with pytest.raises(MCPFailure, match="core_write_denied"):
            await p.prepare(
                replace(
                    call,
                    input_responses={"answer": {"path": str(tmp_path / "core" / "db")}},
                ),
                live=lambda: None,
            )

    asyncio.run(run())
