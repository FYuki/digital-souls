from __future__ import annotations

import asyncio

import pytest

from app.addon_action.dispatch import ActionDispatch
from app.addon_action.journal import ActionJournal, ActionOutcome
from app.external_mcp import Connection, ExecutionContext, ExecutionGate, Registry
from app.external_mcp.models import MCPFailure
from tests.external_mcp_test_support import FakeSource, discovery, manifest
from tests.addon_action_test_support import policy


def test_dispatch_guard_is_checked_before_the_external_send():
    async def scenario() -> None:
        connection = Connection.from_manifest(manifest())
        registry = Registry()
        registry.register(connection)
        source = FakeSource(connection)
        gate = ExecutionGate(registry)
        guard_calls: list[str] = []

        def guard() -> None:
            guard_calls.append("guard")
            raise MCPFailure("policy", "user_stopped")

        async with gate.attach(connection.id, source):
            result = await gate.invoke(
                connection.id,
                "native-tool",
                {"value": 1},
                gate.begin_loop(ExecutionContext("miori", "session-1")),
                dispatch_guard=guard,
            )

        assert result["outcome"] == "cancel_requested"
        assert guard_calls == ["guard"]
        assert source.calls == []

    asyncio.run(scenario())


def test_state_changing_operation_is_not_retried_after_retryable_transport_failure():
    async def scenario() -> None:
        connection = Connection.from_manifest(manifest(trusted=True))
        registry = Registry()
        registry.register(connection)
        source = FakeSource(connection, discovery("write"))
        source.data.tools[0]["annotations"] = {}
        source.failures = [
            MCPFailure("transport", "temporary", retryable=True)
        ]
        gate = ExecutionGate(registry)

        async with gate.attach(connection.id, source):
            result = await gate.invoke(
                connection.id,
                "write",
                {"value": 1},
                gate.begin_loop(ExecutionContext("miori", "session-1")),
            )

        assert result["outcome"] == "failed"
        assert result["retry_count"] == 0
        assert len(source.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("native_result", "error_category", "error_code"),
    [
        (
            {
                "resultType": "input_required",
                "requestState": {},
                "inputRequests": {},
            },
            "protocol",
            "invalid_input_required",
        ),
        (
            {
                "resultType": "input_required",
                "inputRequests": {
                    "answer": {
                        "method": "sampling/createMessage",
                        "params": {"note": "elicitation/create"},
                    }
                },
            },
            "policy",
            "required_capability_unsupported",
        ),
    ],
)
def test_state_changing_input_required_errors_share_unknown_result_across_boundaries(
    tmp_path,
    native_result: dict[str, object],
    error_category: str,
    error_code: str,
):
    async def scenario() -> None:
        action_policy = policy(tmp_path)
        journal = ActionJournal(action_policy.store)
        actions = ActionDispatch(journal, action_policy.sanitizer)
        connection = Connection.from_manifest(manifest(trusted=True))
        registry = Registry()
        registry.register(connection)
        source = FakeSource(connection, discovery("write"))
        source.data.tools[0]["annotations"] = {}
        source.result = native_result
        gate = ExecutionGate(registry, actions=actions)
        context = ExecutionContext("miori", "session", action_scope="activity")

        async with gate.attach(connection.id, source):
            result = await gate.invoke(
                connection.id,
                "write",
                {"value": 1},
                gate.begin_loop(context),
            )

        assert result["outcome"] == "result_unknown"
        assert result["error_category"] == error_category
        assert result["native_error"] == {"code": error_code}
        assert result["dispatch_started"] is True
        assert "interaction_id" not in result
        assert len(source.calls) == 1
        record = journal.get(result["execution_id"])
        assert record.outcome is ActionOutcome.RESULT_UNKNOWN
        assert record.projection == {
            "outcome": "result_unknown",
            "error": error_category,
        }
        assert action_policy.sanitizer.result(result) == {
            "outcome": "result_unknown",
            "error": error_category,
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("requests", [None, False, 0, "", [], {}])
def test_falsy_input_requests_preserve_empty_pending_contract(requests):
    # 基点Gateの空値正規化を保持し、抽出時に新しい拒否条件を追加しない。
    async def scenario() -> None:
        connection = Connection.from_manifest(manifest())
        registry = Registry()
        registry.register(connection)
        source = FakeSource(connection)
        source.result = {"resultType": "input_required", "inputRequests": requests}
        gate = ExecutionGate(registry)
        async with gate.attach(connection.id, source):
            result = await gate.invoke(
                connection.id, "native-tool", {"value": 1},
                gate.begin_loop(ExecutionContext("miori", "session-1")),
            )
            assert result["outcome"] == "input_required"
            assert gate._pending[result["interaction_id"]].request_ids == frozenset()
        assert len(source.calls) == 1
        assert "error_category" not in result
    asyncio.run(scenario())


@pytest.mark.parametrize("payload", [
    {"resultType": "input_required", "requestState": {}, "inputRequests": {}},
    {"resultType": "input_required", "inputRequests": {
        "answer": {"method": "sampling/createMessage"},
    }},
])
def test_input_required_rechecks_liveness_before_classifying_invalid_payload(payload):
    async def scenario() -> None:
        connection = Connection.from_manifest(manifest())
        registry = Registry()
        registry.register(connection)
        source = FakeSource(connection)
        source.result = payload
        gate = ExecutionGate(registry)
        original_live = gate._live

        def live(*args):
            if source.calls:
                raise MCPFailure("policy", "user_stopped")
            return original_live(*args)

        gate._live = live
        async with gate.attach(connection.id, source):
            result = await gate.invoke(
                connection.id, "native-tool", {"value": 1},
                gate.begin_loop(ExecutionContext("miori", "session-1")),
            )
        assert result["outcome"] == "cancel_requested"
        assert result["native_error"] == {"code": "user_stopped"}
        assert "interaction_id" not in result
        assert len(source.calls) == 1
    asyncio.run(scenario())
