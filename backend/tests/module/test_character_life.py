import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from app.character_life.models import Kind, LifeState, Result, now
from app.character_life.runtime import Runtime, Settings
from app.character_life.service import Service, ELYTH_ENDPOINT
from app.character_life.store import Store
from app.external_mcp import Connection, ExecutionGate, Registry
from app.privacy.contracts import ScanSuccess
from app.tool_use.projection import Sanitizer
from tests.external_mcp_test_support import FakeSource, discovery, manifest


class Scanner:
    def scan(self, text):
        return ScanSuccess(())


class Privacy:
    def __init__(self):
        self.allowed_value = True
        self.before_return = None

    async def allowed(self, text):
        if self.before_return:
            await self.before_return()
        return self.allowed_value


class Cognition:
    def __init__(self):
        self.contexts = []

    async def decide(self, context, cancellation):
        self.contexts.append(context)
        if context["results"]:
            return {
                "action": "finish",
                "candidate_id": "",
                "arguments_json": "",
                "summary": "色彩についての公開話題を見つけた",
            }
        return {
            "action": "call",
            "candidate_id": context["candidates"][0]["id"],
            "arguments_json": '{"value":1}',
            "summary": "",
        }


@asynccontextmanager
async def environment(
    tmp_path, *, trusted=False, names=("get_information",), busy=False
):
    connection = Connection.from_manifest(
        manifest(
            trusted=trusted,
            connection_id="elyth",
            transport="streamable_http",
            endpoint=ELYTH_ENDPOINT,
        )
    )
    registry = Registry()
    registry.register(connection)
    gate = ExecutionGate(registry)
    source = FakeSource(connection, discovery(*names))
    store = Store(tmp_path / "life.db")
    cognition, privacy = Cognition(), Privacy()
    service = Service(
        store,
        gate,
        cognition,
        privacy,
        Sanitizer(Scanner()),
        foreground_busy=lambda: busy,
    )
    state = store.save_state(
        LifeState(
            character_id="miori",
            kind=Kind.GOAL_INTENTION,
            content="色彩の話題を探す",
            target_id="elyth",
            source="user",
        )
    )
    grant = store.set_grant("miori", "elyth", connection.identity, True)
    run = store.create_run(state, grant, "request", True)
    async with gate.attach("elyth", source):
        yield service, source, run


def test_exploration_through_real_gate_and_dependency_status(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            assert await service.execute(str(run.id)) == "APPLIED"
            assert await service.execute(str(run.id)) == "APPLIED"
            assert len(source.calls) == 1
            final = service.store.run(str(run.id))
            assert final.dependency_results == {
                "episode": "DEFERRED",
                "personality": "DEFERRED",
            }
            assert (
                len(
                    [
                        s
                        for s in service.store.states("miori")
                        if s.kind is Kind.SHARE_CANDIDATE
                    ]
                )
                == 1
            )
            assert not service.gate._loops

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["revoke", "privacy", "foreground", "interest"])
def test_policy_blocks_external_dispatch(tmp_path, reason):
    async def scenario():
        async with environment(tmp_path, busy=reason == "foreground") as (
            service,
            source,
            run,
        ):
            if reason == "privacy":
                service.privacy.allowed_value = False
            if reason == "revoke":
                service.store.set_grant(
                    "miori", "elyth", source.connection.identity, False
                )
            if reason == "interest":
                state = service.store.state("miori", run.state_id)
                service.store.save_state(
                    state.model_copy(update={"kind": Kind.INTEREST}),
                    expected_revision=1,
                )
            assert await service.execute(str(run.id)) != "APPLIED"
            assert source.calls == []

    asyncio.run(scenario())


def test_revocation_while_waiting_for_gate_lock_prevents_call(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            reached = asyncio.Event()

            async def reviewed():
                reached.set()

            service.privacy.before_return = reviewed
            async with service.gate._locks["elyth"].hold(False):
                task = asyncio.create_task(service.execute(str(run.id)))
                await asyncio.wait_for(reached.wait(), 2)
                await asyncio.sleep(0)
                service.store.set_grant(
                    "miori", "elyth", source.connection.identity, False
                )
            assert await task == "REJECTED"
            assert not source.calls

    asyncio.run(scenario())


def test_pause_during_remote_read_discards_late_result(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            entered, release = asyncio.Event(), asyncio.Event()
            original = source.call_tool

            async def delayed(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            source.call_tool = delayed
            task = asyncio.create_task(service.execute(str(run.id)))
            await entered.wait()
            service.pause(str(run.id))
            release.set()
            assert await task == "DEFERRED"
            assert service.store.run(str(run.id)).phase == "paused"
            assert all(
                s.kind is not Kind.SHARE_CANDIDATE
                for s in service.store.states("miori")
            )

    asyncio.run(scenario())


def test_elyth_write_dm_and_notifications_never_enter_catalog(tmp_path):
    async def scenario():
        async with environment(
            tmp_path, names=("create_post", "get_dm_thread", "mark_notifications_read")
        ) as (service, source, run):
            assert await service.execute(str(run.id)) == "DEFERRED"
            assert source.calls == []
            assert service.cognition.contexts == []

    asyncio.run(scenario())


def test_dbos_execution_relaunch_dedup_and_no_missed_cron(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            runtime = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            await runtime.start()
            try:
                for _ in range(100):
                    if service.store.run(str(run.id)).phase == "finished":
                        break
                    await asyncio.sleep(0.05)
                assert service.store.run(str(run.id)).result is Result.APPLIED
                assert await runtime.scan((now() - timedelta(days=1)).isoformat()) == 0
            finally:
                await runtime.close()
            assert not runtime.started
            service.closing = False
            restarted = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            await restarted.start()
            try:
                await restarted.enqueue(run)
                await asyncio.sleep(0.1)
                assert len(source.calls) == 1
                assert service.store.run(str(run.id)).result is Result.APPLIED
            finally:
                await restarted.close()

    asyncio.run(scenario())


def test_process_crash_after_domain_commit_does_not_repeat_read_or_state(tmp_path):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2]
    worker = backend / "tests/fixtures/character_life/crash_worker.py"
    env = {**os.environ, "PYTHONPATH": str(backend)}
    first = subprocess.run(
        [sys.executable, str(worker), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert first.returncode == 23, first.stderr
    second = subprocess.run(
        [sys.executable, str(worker), str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert second.returncode == 0, second.stderr
    evidence = json.loads(second.stdout.strip())
    assert evidence == {
        "result": "APPLIED",
        "shares": 1,
        "reads": 1,
        "workflow": "SUCCESS",
    }


def test_stale_workflow_attempt_does_not_dispatch(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            service.store.save_run(
                run.model_copy(update={"attempt": 2}), expected_attempt=1
            )
            assert await service.execute(str(run.id), attempt=1) == "SUPERSEDED"
            assert source.calls == []
            assert service.store.run(str(run.id)).phase == "queued"

    asyncio.run(scenario())


def test_failed_tool_result_alone_cannot_create_share_candidate(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):

            async def unavailable(*args, **kwargs):
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "PRIVATE_ERROR"}],
                }

            source.call_tool = unavailable
            assert await service.execute(str(run.id)) == "NO_CHANGE"
            assert all(
                s.kind is not Kind.SHARE_CANDIDATE
                for s in service.store.states("miori")
            )
            assert "PRIVATE_ERROR" not in str(service.cognition.contexts)

    asyncio.run(scenario())


def test_shutdown_completes_domain_deferral_before_destroying_dbos(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            entered = asyncio.Event()

            async def reading(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()

            source.call_tool = reading
            runtime = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            await runtime.start()
            await asyncio.wait_for(entered.wait(), 10)
            await asyncio.wait_for(runtime.close(), 15)
            final = service.store.run(str(run.id))
            assert final.result is Result.DEFERRED
            assert final.reason == "runtime_stopping"
            assert final.phase == "finished"
            assert not service.tasks
            assert not service.cancellations
            assert not service.gate._loops
            assert not runtime.futures

    asyncio.run(scenario())
