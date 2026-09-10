"""会話外の副作用が確認キュー・永続送信記録を通り、同じ活動を再送しない。"""

import asyncio
from types import SimpleNamespace

from app.addon_action.dispatch import ActionDispatch
from app.addon_action.journal import ActionJournal
from app.addon_action.models import ApprovalChoice, ExecutionScene
from app.addon_action.recovery import ActionRecovery
from app.character_life import service as module
from app.external_mcp.models import MCPFailure
from tests.module.test_character_life import environment, Cognition
from tests.unit.test_addon_action_queue import policy


def connect(service, tmp_path):
    class WriteCognition(Cognition):
        async def decide(self, context, cancellation):
            ordered = {
                **context,
                "candidates": sorted(
                    context["candidates"], key=lambda c: c["name"] != "write"
                ),
            }
            return await super().decide(ordered, cancellation)

    service.cognition = WriteCognition()
    p = policy(tmp_path, autonomous_wait_seconds=0.02)
    service.gate.confirmations = p
    service.gate.actions = ActionDispatch(ActionJournal(p.store), p.sanitizer)
    recovery = ActionRecovery(service.gate, service.gate.actions)
    service.gate.recovery = service.gate.tasks = recovery
    recovery.autonomous_guard = service.validate_recovery
    return p


def test_expired_activity_and_late_once_affect_only_future_use(tmp_path):
    async def run():
        async with environment(
            tmp_path, names=("write",), endpoint="http://localhost:9100/mcp"
        ) as (service, source, activity):
            p = connect(service, tmp_path)
            assert await service.execute(str(activity.id)) == "DEFERRED"
            request = p.store.requests()[0]
            assert (
                request.key.scene == ExecutionScene.AUTONOMOUS and not request.waiting
            )
            assert not source.calls
            p.store.answer(request.id, ApprovalChoice.ONCE)
            assert await service.execute(str(activity.id)) == "DEFERRED"
            assert not source.calls
            next_run = service.store.create_run(
                service.store.state("miori", activity.state_id),
                service.store.grant("miori", "elyth"),
                "new-user-request",
                True,
            )
            assert await service.execute(str(next_run.id)) == "APPLIED"
            assert len(source.calls) == 1
            assert p.store.state(request.key).remaining == 0

    asyncio.run(run())


def test_approval_wait_rechecks_egress_without_old_thirty_second_cutoff(
    tmp_path, monkeypatch
):
    async def run():
        async with environment(
            tmp_path, names=("write",), endpoint="http://localhost:9100/mcp"
        ) as (service, source, activity):
            p = connect(service, tmp_path)
            p.autonomous_wait_seconds = 2
            seconds = [100.0]
            monkeypatch.setattr(
                module, "time", SimpleNamespace(monotonic=lambda: seconds[0])
            )
            checked = []

            async def egress(arguments):
                checked.append(seconds[0])
                return True

            p.egress = egress
            task = asyncio.create_task(service.execute(str(activity.id)))
            async with asyncio.timeout(2):
                while not p.store.requests():
                    await asyncio.sleep(0.005)
            seconds[0] = 145.0
            p.store.answer(p.store.requests()[0].id, ApprovalChoice.ONCE)
            assert await task == "APPLIED"
            assert checked[0] == 100 and checked[-1] == 145
            assert len(source.calls) == 1

    asyncio.run(run())


def test_runtime_attempt_change_does_not_repeat_uncertain_side_effect(tmp_path):
    async def run():
        async with environment(
            tmp_path, names=("write",), endpoint="http://localhost:9100/mcp"
        ) as (service, source, activity):
            p = connect(service, tmp_path)
            # まず待機要求を作り、後続の活動に1回分だけ許可する。
            assert await service.execute(str(activity.id)) == "DEFERRED"
            p.store.answer(p.store.requests()[0].id, ApprovalChoice.ONCE)
            current = service.store.create_run(
                service.store.state("miori", activity.state_id),
                service.store.grant("miori", "elyth"),
                "write-request",
                True,
            )
            source.failures = [
                MCPFailure("transport", "lost_response", request_started=True)
            ]
            assert await service.execute(str(current.id)) == "RESULT_UNKNOWN"
            assert len(source.calls) == 1
            restored = current.model_copy(
                update={"attempt": 2, "phase": "queued", "result": None}
            )
            service.store.save_run(restored, expected_attempt=1)
            previous_decisions = len(service.cognition.contexts)
            assert await service.execute(str(current.id), attempt=2) == "RESULT_UNKNOWN"
            assert len(source.calls) == 1 and len(p.store.requests()) == 1
            assert len(service.cognition.contexts) == previous_decisions

    asyncio.run(run())
