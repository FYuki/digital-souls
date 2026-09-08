"""実ELYTH・実Ollama・実DBOSによるdev/test専用の話題探索受入。"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest
from dbos import DBOS
from jsonschema import Draft202012Validator

from app.character_life.cognition import Cognition, Privacy
from app.character_life.models import Kind, LifeState, Result
from app.character_life.prompt import Context
from app.character_life.runtime import Runtime, SCHEDULE, Settings
from app.character_life.service import ELYTH_TOPIC_TOOLS, Service
from app.character_life.store import Store
from app.external_mcp import ExecutionGate, ExternalMCPClient, Registry
from app.inference.runtime import create_inference_runtime
from app.inference import InferenceCaller, InferenceMessage, InferenceTarget
from app.prompting import BuiltPrompt, PromptMessage, PromptRole
from app.prompting.models import PromptUsage
from app.memory.memory_policy import resolved_memory_policy
from app.privacy.scanner import create_privacy_scanner
from app.privacy.semantic.classifier import InferenceSemanticPrivacyClassifier
from app.privacy.semantic.inference_client import InferenceSemanticClassifierClient
from app.tool_use.projection import Sanitizer
from app.tool_use.runtime import ToolSettings
from tests.fixtures.character_life.http_server import life_http
from tests.fixtures.character_life.priority_benchmark import measure_priority


@pytest.mark.inference_real
def test_elyth_topic_exploration_real_services(tmp_path, monkeypatch):
    if os.environ.get("RUN_CHARACTER_LIFE_REAL_TESTS") != "true":
        pytest.skip("explicit real-service acceptance only")
    assert os.environ.get("ELYTH_API_KEY"), "ELYTH_API_KEY must be configured locally"
    assert os.environ.get("DS_ENVIRONMENT_ID") == "test"
    model = os.environ.get("CHARACTER_LIFE_ACCEPTANCE_MODEL", "gemma4:e4b")
    environment = {
        "OLLAMA_BASE_URL": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    }
    for target in (
        "CHAT",
        "PRIVACY",
        "MEMORY_EXTRACTION",
        "MEMORY_CONSOLIDATION",
        "CHARACTER_LIFE",
    ):
        environment[f"INFERENCE_TARGET_{target}"] = f"ollama/{model}"
        environment[f"INFERENCE_TARGET_{target}_MAX_INPUT_TOKENS"] = "12000"
        environment[f"INFERENCE_TARGET_{target}_MAX_OUTPUT_TOKENS"] = (
            "1024" if target in {"CHARACTER_LIFE", "CHAT"} else "512"
        )
        environment[f"INFERENCE_TARGET_{target}_TIMEOUT_SECONDS"] = "60"
        environment[f"INFERENCE_TARGET_{target}_OPTIONS_JSON"] = '{"temperature":0}'
    environment["INFERENCE_TARGET_EMBEDDING"] = "ollama/nomic-embed-text:latest"
    environment["INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS"] = "8192"
    inference = create_inference_runtime(environment)
    policy = resolved_memory_policy()
    client = InferenceSemanticClassifierClient(
        router=inference.router,
        settings=inference.settings,
        model_digest_resolver=lambda name, timeout: (
            inference.ollama_adapter.resolve_model_digest(name, timeout_seconds=timeout)
        ),
    )
    classifier = InferenceSemanticPrivacyClassifier(
        client=client,
        privacy_policy=policy.privacy,
        model_id=model,
        model_digest_resolver=lambda timeout: client.resolve_model_digest(
            timeout_seconds=timeout
        ),
    )
    scanner = create_privacy_scanner(policy.privacy)
    sanitizer = Sanitizer(scanner, secret_references=("ELYTH_API_KEY",))
    root = Path(__file__).resolve().parents[3]
    connection = ToolSettings.load(
        str(root / "backend/config/character-life-elyth.example.json")
    ).connections[0]
    report = {
        "scenario": "elyth-topic-exploration",
        "model": model,
        "chat_max_output_tokens": int(environment["INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS"]),
        "external_calls": [],
        "decisions": [],
        "status": "failed",
    }

    async def scenario():
        registry = Registry()
        registry.register(connection)
        gate = ExecutionGate(registry)
        source = ExternalMCPClient(connection, timeout=30)
        original = source.call_tool

        async def tracked(name, arguments, **kwargs):
            assert name in ELYTH_TOPIC_TOOLS
            response = await original(name, arguments, **kwargs)
            report["external_calls"].append(name)
            return response

        source.call_tool = tracked
        async with source.connect(), gate.attach(connection.id, source):
            store = Store(tmp_path / "life.db")
            cognition = Cognition(inference.router)
            original_decide = cognition.decide

            async def tracked_decision(context, cancellation):
                decision = await original_decide(context, cancellation)
                candidate = next(
                    (
                        c
                        for c in context["candidates"]
                        if c["id"] == decision["candidate_id"]
                    ),
                    None,
                )
                diagnostic = {"action": decision["action"]}
                if candidate is not None and candidate["name"] in ELYTH_TOPIC_TOOLS:
                    diagnostic["tool"] = candidate["name"]
                    try:
                        arguments = json.loads(decision["arguments_json"])
                        diagnostic["schema_errors"] = [
                            e.validator
                            for e in Draft202012Validator(
                                candidate["input_schema"]
                            ).iter_errors(arguments)
                        ]
                    except ValueError:
                        diagnostic["schema_errors"] = ["invalid_json"]
                report["decisions"].append(diagnostic)
                return decision

            cognition.decide = tracked_decision
            service = Service(
                store,
                gate,
                cognition,
                Privacy(sanitizer, classifier),
                sanitizer,
                foreground_busy=lambda: False,
                timeout=240,
            )
            state = store.save_state(
                LifeState(
                    character_id="miori",
                    kind=Kind.GOAL_INTENTION,
                    content="ELYTHで今話題の公開情報を読み、次の会話で共有できる話題を一つ探す。",
                    target_id=connection.id,
                    source="user",
                )
            )
            store.set_grant("miori", connection.id, connection.identity, True)
            runtime = Runtime(service, tmp_path, Settings(True, "0 0 1 1 *"))
            await runtime.start()
            try:
                async with life_http(runtime) as http:
                    status = await http.get("/character-life/miori")
                    assert status.status_code == 200
                    assert status.headers["Cache-Control"] == "no-store"
                    schedule = await asyncio.to_thread(DBOS.trigger_schedule, SCHEDULE)
                    async with asyncio.timeout(15):
                        while not store.runs("miori"):
                            await asyncio.sleep(0.05)
                    run = store.runs("miori")[0]
                    assert not run.requested
                    assert run.request_id.startswith("schedule:")
                    report["trigger"] = "dbos_schedule"
                    report["autonomous"] = True
                report["http_connectivity"] = "passed"
                report["http_server_closed"] = True
                for _ in range(500):
                    final = store.run(str(run.id))
                    if final.phase == "finished":
                        break
                    await asyncio.sleep(0.5)
                report["result"] = final.result
                report["reason"] = final.reason
                report["dependencies"] = final.dependency_results
                assert final.result is Result.APPLIED, final.reason
                schedule_status = await asyncio.to_thread(
                    DBOS.get_workflow_status, schedule.get_workflow_id()
                )
                assert schedule_status is not None and schedule_status.status == "SUCCESS"
                report["schedule_result"] = schedule_status.status
                shares = [
                    s for s in store.states("miori") if s.kind is Kind.SHARE_CANDIDATE
                ]
                assert len(shares) == 1
                assert report["external_calls"]
                count = len(report["external_calls"])
                replay = await runtime.submit(
                    "miori", state.id, run.request_id, requested=False
                )
                assert replay.id == run.id
                await asyncio.sleep(0.1)
                assert len(report["external_calls"]) == count
                report["shared_candidate_count"] = len(shares)
                report["idempotency"] = "passed"
                prompt = BuiltPrompt(
                    (
                        PromptMessage(
                            PromptRole.SYSTEM,
                            "あなたは光織です。日本語で短く自然に答えてください。",
                        ),
                        PromptMessage(
                            PromptRole.USER,
                            "前に見つけた話題を一つ、出典のサービス名を添えて教えて。",
                        ),
                    ),
                    PromptUsage(0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
                    (),
                )
                projected = Context(
                    store,
                    lambda messages: sum(len(m.content.encode()) for m in messages),
                    12000,
                )("miori", prompt)
                life_context = json.loads(
                    projected.messages[-2].content.split("\n", 1)[1].rsplit("\n", 1)[0]
                )
                share_is_in_context = any(
                    item.get("kind") == Kind.SHARE_CANDIDATE
                    and item.get("content") == shares[0].content
                    for item in life_context
                )
                # JSONの改行escapeを復元して完全一致を確認し、失敗時も実本文を表示しない。
                assert share_is_in_context, "approved_share_missing_from_context"
                started = time.monotonic()
                first_delta = None
                response_parts = []
                async for delta in inference.router.stream_text(
                    caller=InferenceCaller.CHAT,
                    target=InferenceTarget.CHAT,
                    messages=tuple(
                        InferenceMessage(m.role.value, m.content)
                        for m in projected.messages
                    ),
                ):
                    if delta and first_delta is None:
                        first_delta = time.monotonic() - started
                    response_parts.append(delta)
                assert first_delta is not None
                mentions_source = "elyth" in "".join(response_parts).lower()
                assert mentions_source, "source_service_missing_from_response"
                report["next_conversation"] = (
                    "life_state_context_and_real_response_passed"
                )
                report["chat_ttft_seconds"] = round(first_delta, 3)
                report["chat_duration_seconds"] = round(time.monotonic() - started, 3)
                if os.environ.get("RUN_CHARACTER_LIFE_PRIORITY_BENCHMARK") == "true":
                    report["priority_benchmark"] = await measure_priority(
                        runtime,
                        state,
                        inference.router,
                        projected,
                        lambda: len(report["external_calls"]),
                    )
                report["status"] = "passed"
            finally:
                await runtime.close()
                report["runtime_closed"] = not runtime.started
        report["mcp_closed"] = not source.connected

    try:
        inference.probe_startup()
        asyncio.run(scenario())
    finally:
        inference.close()
        report_path = os.environ.get("CHARACTER_LIFE_ACCEPTANCE_REPORT")
        if report_path:
            path = Path(report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
