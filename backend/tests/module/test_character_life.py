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
    tmp_path,
    *,
    trusted=False,
    names=("get_information",),
    busy=False,
    endpoint=ELYTH_ENDPOINT,
):
    connection = Connection.from_manifest(
        manifest(
            trusted=trusted,
            connection_id="elyth",
            transport="streamable_http",
            endpoint=endpoint,
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


@pytest.mark.parametrize("interruption", ["foreground", "requested"])
@pytest.mark.parametrize("stage", ["catch_up", "formation", "privacy"])
def test_formation_yields_when_higher_priority_work_arrives(
    tmp_path, interruption, stage
):
    from uuid import uuid4

    from app.character_life.formation import LifeFormation
    from app.character_life.models import ReflectionView
    from app.character_life.ports import DeferredMemory

    async def scenario():
        async with environment(tmp_path) as (service, _, run):
            # fixtureの利用者要求は完了済みとし、形成中に次の要求を投入する。
            service.store.finish(run, Result.NO_CHANGE, "test_setup")
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []
            reflection = ReflectionView(
                id=uuid4(), character_id="miori", revision="1",
                content="色彩に関心を感じた", active=True,
            )

            async def checkpoint(name):
                calls.append(name)
                if name == stage:
                    entered.set()
                    await release.wait()

            class Memory(DeferredMemory):
                async def catch_up(self, character):
                    await checkpoint("catch_up")
                    return Result.NO_CHANGE

            class Source:
                async def active(self, character):
                    return (reflection,)

                def current_revisions(self, character):
                    return {reflection.id: reflection.revision}

            class Proposal:
                async def form(self, reflections, cancellation):
                    await checkpoint("formation")
                    return {"states": [{
                        "kind": "INTEREST", "content": "色彩への関心",
                        "source_ids": [str(reflection.id)],
                    }]}

            class Admission:
                async def allowed(self, text):
                    await checkpoint("privacy")
                    return True

            service.memory = Memory()
            service.formation = LifeFormation(
                service.store, Source(), Proposal(), Admission()
            )
            task = asyncio.create_task(service.form_life_states("miori"))
            await asyncio.wait_for(entered.wait(), 2)
            if interruption == "foreground":
                service.foreground_busy = lambda: True
            else:
                state = service.store.state("miori", run.state_id)
                grant = service.store.grant("miori", "elyth")
                requested = service.store.create_run(state, grant, "next", True)
            release.set()
            assert await task == Result.DEFERRED
            assert calls[-1] == stage
            assert not any(s.source == "reflection" for s in service.store.states("miori"))
            if interruption == "foreground":
                service.foreground_busy = lambda: False
            else:
                service.store.finish(requested, Result.NO_CHANGE, "test_complete")
            # 保留では形成済みledgerを消費せず、後のscanで同じ正本を再評価できる。
            assert await service.form_life_states("miori") == Result.APPLIED
            assert len([s for s in service.store.states("miori") if s.source == "reflection"]) == 1

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
            await asyncio.wait_for(entered.wait(), 2)
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


@pytest.mark.parametrize(
    "crash_at,exit_code", [("domain_commit", 23), ("memory_commit", 24)]
)
def test_process_crash_after_domain_commit_does_not_repeat_read_or_state(
    tmp_path, crash_at, exit_code
):
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2]
    worker = backend / "tests/fixtures/character_life/crash_worker.py"
    env = {**os.environ, "PYTHONPATH": str(backend)}
    first = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), crash_at],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert first.returncode == exit_code, first.stderr
    second = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), crash_at],
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


def test_scheduler_scans_registered_characters_and_publishes_deferred_formation(
    tmp_path,
):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            service.pause(str(run.id))
            runtime = Runtime(
                service,
                tmp_path,
                Settings(True, "0 0 1 1 *"),
                characters=lambda: ("miori", "other"),
            )
            await runtime.start()
            try:
                scheduled = now().isoformat()
                await runtime.scan(scheduled)
                for _ in range(100):
                    jobs = service.store.formation_jobs("other")
                    if jobs and jobs[0]["result"] is not None:
                        break
                    await asyncio.sleep(0.05)
                assert jobs[0]["result"] == "DEFERRED"
                count = len(jobs)
                await runtime.submit_formation("other", scheduled)
                assert len(service.store.formation_jobs("other")) == count
                assert all(
                    j["character_id"] == "miori"
                    for j in service.store.formation_jobs("miori")
                )
            finally:
                await runtime.close()

    asyncio.run(scenario())


def test_dbos_checkpoint_contains_references_and_results_without_domain_text(tmp_path):
    import sqlite3

    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            runtime = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            state_text = service.store.state("miori", run.state_id).content
            await runtime.start()
            try:
                for _ in range(100):
                    if service.store.run(str(run.id)).phase == "finished":
                        break
                    await asyncio.sleep(0.05)
                assert service.store.run(str(run.id)).result is Result.APPLIED
            finally:
                await runtime.close()
            with sqlite3.connect(runtime.system_path) as db:
                dump = "\n".join(db.iterdump())
            assert str(run.id) in dump
            assert state_text not in dump
            assert "色彩についての公開話題を見つけた" not in dump
            assert "arguments_json" not in dump
            assert "native_payload" not in dump

    asyncio.run(scenario())


def test_unclassified_effect_explicitly_defers_until_action_recovery_is_connected(
    tmp_path,
):
    async def scenario():
        async with environment(tmp_path, endpoint="https://example.test/mcp") as (
            service,
            source,
            run,
        ):

            async def decide(context, cancellation):
                c = next(c for c in context["candidates"] if c["kind"] == "tool")
                return {
                    "action": "call",
                    "candidate_id": c["id"],
                    "arguments_json": '{"value":1}',
                    "summary": "",
                }

            service.cognition.decide = decide
            assert await service.execute(str(run.id)) == "DEFERRED"
            assert (
                service.store.run(str(run.id)).reason == "action_recovery_unavailable"
            )
            assert not source.calls

    asyncio.run(scenario())


def test_native_resource_read_uses_execution_gate(tmp_path):
    async def scenario():
        async with environment(tmp_path, endpoint="https://example.test/mcp") as (
            service,
            source,
            run,
        ):

            async def decide(context, cancellation):
                if context["results"]:
                    return {
                        "action": "finish",
                        "candidate_id": "",
                        "arguments_json": "",
                        "summary": "資料から得た話題",
                    }
                c = next(c for c in context["candidates"] if c["kind"] == "resource")
                return {
                    "action": "call",
                    "candidate_id": c["id"],
                    "arguments_json": "{}",
                    "summary": "",
                }

            service.cognition.decide = decide
            assert await service.execute(str(run.id)) == "APPLIED"
            assert len(source.calls) == 1
            assert source.calls[0][0] == "test://resource"

    asyncio.run(scenario())


def test_binding_constraints_are_applied_before_schema_and_egress(tmp_path):
    from app.tool_use.binding import BindingResolver, BindingTarget

    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            resolver = BindingResolver(
                (
                    BindingTarget(
                        "public",
                        "elyth",
                        "miori",
                        "公開資料",
                        ("get_information",),
                        '{"value":2}',
                    ),
                )
            )
            service.bindings = resolver
            service.gate.bindings = resolver
            original = service.cognition.decide

            async def decide(context, cancellation):
                result = await original(context, cancellation)
                if result["action"] == "call":
                    result["arguments_json"] = "{}"
                return result

            service.cognition.decide = decide
            assert await service.execute(str(run.id)) == "APPLIED"
            assert source.calls[0][1] == {"value": 2}
            assert resolver._resolved == {}

    asyncio.run(scenario())


def test_paused_activity_resumes_with_new_attempt_and_one_publication(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            service.pause(str(run.id))
            runtime = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            await runtime.start()
            try:
                resumed = await runtime.resume(str(run.id))
                assert resumed.attempt == 2
                for _ in range(100):
                    if service.store.run(str(run.id)).phase == "finished":
                        break
                    await asyncio.sleep(0.05)
                assert service.store.run(str(run.id)).result is Result.APPLIED
                assert len(source.calls) == 1
                assert (await runtime.resume(str(run.id))).attempt == 2
            finally:
                await runtime.close()

    asyncio.run(scenario())


def test_duplicate_model_reads_are_suppressed_before_final_synthesis(tmp_path):
    async def scenario():
        async with environment(tmp_path) as (service, source, run):

            async def decide(context, cancellation):
                if context["finalize_only"]:
                    return {
                        "action": "finish",
                        "candidate_id": "",
                        "arguments_json": "",
                        "summary": "取得済みの公開話題",
                    }
                return {
                    "action": "call",
                    "candidate_id": context["candidates"][0]["id"],
                    "arguments_json": '{"value":1}',
                    "summary": "",
                }

            service.cognition.decide = decide
            assert await service.execute(str(run.id)) == "APPLIED"
            assert len(source.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failed_dependency", ["memory", "personality"])
@pytest.mark.parametrize("unfinished", [Result.FAILED, Result.RESULT_UNKNOWN, Result.DEFERRED])
def test_dependency_failure_resumes_with_same_owned_handoff(
    tmp_path, failed_dependency, unfinished
):
    from app.character_life.ports import DeferredMemory

    async def scenario():
        async with environment(tmp_path) as (service, source, run):
            observations, evaluations = [], []

            class Memory(DeferredMemory):
                async def record_observation(self, **kwargs):
                    observations.append(kwargs)
                    if len(observations) == 1:
                        if failed_dependency == "memory":
                            if unfinished is Result.FAILED:
                                raise RuntimeError("temporary failure")
                            return unfinished
                        return Result.APPLIED
                    return Result.NO_CHANGE

            class Personality:
                async def evaluate(self, character, request_id):
                    evaluations.append((character, request_id))
                    if failed_dependency == "personality" and len(evaluations) == 1:
                        return unfinished
                    return Result.NO_CHANGE

            service.memory, service.personality = Memory(), Personality()
            assert await service.execute(str(run.id)) == Result.DEFERRED
            failed = service.store.run(str(run.id))
            assert failed.dependency_results[
                "episode" if failed_dependency == "memory" else "personality"
            ] == unfinished
            assert failed.handoff.character_id == run.character_id
            assert not any(
                s.kind is Kind.SHARE_CANDIDATE for s in service.store.states("miori")
            )
            runtime = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            await runtime.start()
            try:
                resumed = await runtime.resume(str(run.id))
                assert resumed.attempt == 2
                async with asyncio.timeout(10):
                    while service.store.run(str(run.id)).phase != "finished":
                        await asyncio.sleep(0.05)
                assert service.store.run(str(run.id)).result is Result.APPLIED
                assert observations[0] == observations[1]
                assert len(source.calls) == 1
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
            finally:
                await runtime.close()

    asyncio.run(scenario())


def test_example_allowlist_applies_to_catalog_and_direct_gate(tmp_path):
    import json
    from pathlib import Path
    from app.external_mcp import ExecutionContext
    from app.tool_use.catalog import catalog
    from app.character_life.service import ELYTH_TOPIC_TOOLS

    async def scenario():
        root = Path(__file__).resolve().parents[3]
        config = json.loads(
            (root / "backend/config/character-life-elyth.example.json").read_text()
        )
        connection = Connection.from_manifest(config["connections"][0])
        assert (
            set(connection.manifest["core_policy"]["operation_allowlist"])
            == ELYTH_TOPIC_TOOLS
        )
        registry = Registry()
        registry.register(connection)
        gate = ExecutionGate(registry)
        source = FakeSource(
            connection, discovery("get_information", "new_private_tool")
        )
        async with gate.attach("elyth", source):
            loop = gate.begin_loop(ExecutionContext("miori", "conversation"))
            try:
                assert [c.name for c in catalog(gate, loop, Sanitizer(Scanner()))] == [
                    "get_information"
                ]
                denied = await gate.invoke(
                    "elyth", "new_private_tool", {"value": 1}, loop
                )
                assert denied["outcome"] == "failed"
                assert not source.calls
                allowed = await gate.invoke(
                    "elyth", "get_information", {"value": 1}, loop
                )
                assert allowed["outcome"] == "succeeded"
                assert len(source.calls) == 1
            finally:
                gate.end_loop(loop)

    asyncio.run(scenario())


def test_gpu_sampler_timeout_is_missing_data_and_restores_callbacks(monkeypatch):
    import subprocess
    import threading
    from types import SimpleNamespace
    from uuid import uuid4
    from tests.fixtures.character_life.priority_benchmark import measure_priority

    async def scenario():
        sampled = threading.Event()

        async def decide(context, cancellation):
            return None

        busy = lambda: False
        service = SimpleNamespace(
            cognition=SimpleNamespace(decide=decide),
            foreground_busy=busy,
            store=SimpleNamespace(
                run=lambda _: SimpleNamespace(
                    phase="finished",
                    result=Result.DEFERRED,
                    reason="foreground_priority",
                )
            ),
        )

        class RuntimeStub:
            async def submit(self, *args, **kwargs):
                await service.cognition.decide({}, None)
                return SimpleNamespace(id=uuid4())

        runtime = RuntimeStub()
        runtime.service = service

        class Router:
            async def stream_text(self, **kwargs):
                async with asyncio.timeout(2):
                    while not sampled.is_set():
                        await asyncio.sleep(0.01)
                yield "応答"

        def timeout(*args, **kwargs):
            sampled.set()
            raise subprocess.TimeoutExpired("synthetic-gpu", 3)

        monkeypatch.setattr(
            "tests.fixtures.character_life.priority_benchmark.shutil.which",
            lambda _: "synthetic-gpu",
        )
        monkeypatch.setattr(
            "tests.fixtures.character_life.priority_benchmark.subprocess.run", timeout
        )
        result = await measure_priority(
            runtime,
            SimpleNamespace(id=uuid4()),
            Router(),
            SimpleNamespace(messages=()),
            lambda: 0,
        )
        assert result["gpu_memory_peak_mib"] is None
        assert result["gpu_utilization_peak_percent"] is None
        assert service.cognition.decide is decide
        assert service.foreground_busy is busy

    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["foreground", "requested", "shutdown", "timeout"])
def test_formation_cancels_inference_before_worker_returns(tmp_path, interruption):
    import threading
    from types import SimpleNamespace
    from uuid import uuid4
    from app.character_life.cognition import Formation
    from app.character_life.formation import LifeFormation
    from app.character_life.models import ReflectionView

    async def scenario():
        async with environment(tmp_path) as (service, _, run):
            service.store.finish(run, Result.NO_CHANGE, "setup")
            loop = asyncio.get_running_loop()
            entered = asyncio.Event()
            release, exited = threading.Event(), threading.Event()
            tokens = []
            reflection = ReflectionView(
                id=uuid4(), character_id="miori", revision="1", content="色彩への内省",
                active=True,
            )
            class Source:
                async def active(self, character):
                    return (reflection,)
                def current_revisions(self, character):
                    return {reflection.id: reflection.revision}
            class Router:
                def generate_structured(self, **kwargs):
                    tokens.append(kwargs["cancellation_token"])
                    loop.call_soon_threadsafe(entered.set)
                    try:
                        assert release.wait(2)
                        return SimpleNamespace(value={"states": [{
                            "kind": "INTEREST", "content": "遅れて返る内省",
                            "source_ids": [str(reflection.id)],
                        }]})
                    finally:
                        exited.set()
            service.formation = LifeFormation(service.store, Source(), Formation(Router()), Privacy())
            if interruption == "timeout":
                service.timeout = 0.15
            task = asyncio.create_task(service.form_life_states("miori"))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                if interruption == "foreground":
                    service.foreground_busy = lambda: True
                elif interruption == "requested":
                    state = service.store.state("miori", run.state_id)
                    grant = service.store.grant("miori", "elyth")
                    service.store.create_run(state, grant, "priority-request", True)
                elif interruption == "shutdown":
                    await service.stop()
                assert await asyncio.wait_for(task, 1) == Result.DEFERRED
                assert tokens[0].is_cancelled
                assert not exited.is_set()
                assert not any(s.source == "reflection" for s in service.store.states("miori"))
            finally:
                release.set()
                assert await asyncio.to_thread(exited.wait, 2)
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            # 同期workerが遅れて返した結果も正本へ反映しない。
            assert not any(s.source == "reflection" for s in service.store.states("miori"))
    asyncio.run(scenario())


def test_real_dbos_cron_enqueues_autonomous_activity_and_formation(tmp_path):
    from dbos import DBOS
    from app.character_life.runtime import SCHEDULE

    async def scenario():
        async with environment(tmp_path) as (service, source, initial):
            service.store.finish(initial, Result.NO_CHANGE, "setup")
            runtime = Runtime(
                service, tmp_path, Settings(True, "*/5 * * * * *"),
                characters=lambda: ("miori",),
            )
            await runtime.start()
            try:
                paused = False
                # scanの直接呼出しでは検出できない、DBOS step contextからの投入を検証する。
                async with asyncio.timeout(20):
                    while True:
                        runs = [r for r in service.store.runs("miori") if not r.requested]
                        if runs and not paused:
                            # 最初の実発火を観測後、次周期の別活動とは分けて検証する。
                            await asyncio.to_thread(DBOS.pause_schedule, SCHEDULE)
                            paused = True
                        jobs = service.store.formation_jobs("miori")
                        schedules = await asyncio.to_thread(
                            DBOS.list_workflows, name="character_life_schedule_v1"
                        )
                        if (
                            runs and runs[0].result is Result.APPLIED
                            and jobs and jobs[0]["result"] == Result.DEFERRED
                            and schedules and all(w.status == "SUCCESS" for w in schedules)
                        ):
                            break
                        await asyncio.sleep(0.05)
                service.foreground_busy = lambda: True
                assert runs[0].request_id.startswith("schedule:")
                assert len(source.calls) == 1
                assert len([
                    s for s in service.store.states("miori") if s.kind is Kind.SHARE_CANDIDATE
                ]) == 1
            finally:
                await runtime.close()
            assert not runtime.started
    asyncio.run(scenario())
