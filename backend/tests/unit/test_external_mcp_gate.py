"""#156の単体検証。"""

import asyncio
import pytest
from app.external_mcp import (
    Connection,
    ExecutionContext,
    ExecutionGate,
    MCPFailure,
    RateLimits,
    Registry,
)
from tests.external_mcp_test_support import FakeSource, discovery, manifest

CTX = ExecutionContext("miori", "test-session")


def test_snapshot_activation_and_offline():
    async def run():
        c = Connection.from_manifest(manifest())
        r = Registry()
        r.register(c)
        gate = ExecutionGate(r)
        source = FakeSource(c)
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(CTX)
            source.data = discovery("added")
            staged = await gate.refresh(c.id)
            assert staged.document["activation_state"] == "staged"
            assert (await gate.invoke(c.id, "added", {"value": 1}, loop))[
                "outcome"
            ] == "failed"
            assert (await gate.invoke(c.id, "native-tool", {"value": 1}, loop))[
                "outcome"
            ] == "succeeded"
            next_loop = gate.begin_loop(CTX)
            assert (await gate.invoke(c.id, "added", {"value": 1}, next_loop))[
                "outcome"
            ] == "succeeded"
            assert (await gate.invoke(c.id, "native-tool", {"value": 1}, next_loop))[
                "outcome"
            ] == "failed"
        assert r.entry(c.id).active is not None
        assert (await gate.invoke(c.id, "added", {"value": 2}, next_loop))[
            "outcome"
        ] == "failed"
        async with gate.attach(c.id, source):
            assert (await gate.invoke(c.id, "added", {"value": 3}, next_loop))[
                "outcome"
            ] == "failed"

    asyncio.run(run())


@pytest.mark.parametrize("trusted,expected", [(False, 1), (True, 2)])
def test_only_effective_read_retries(trusted, expected):
    async def run():
        c = Connection.from_manifest(manifest(trusted=trusted))
        r = Registry()
        r.register(c)
        gate = ExecutionGate(r)
        source = FakeSource(c)
        source.failures = [MCPFailure("transport", "failure", retryable=True)]
        async with gate.attach(c.id, source):
            result = await gate.invoke(
                c.id, "native-tool", {"value": 1}, gate.begin_loop(CTX)
            )
            assert len(source.calls) == expected
            assert result["retry_count"] == expected - 1
            assert result["outcome"] == ("succeeded" if trusted else "failed")

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["identical", "same", "total", "cycle", "rate"])
def test_budgets(mode):
    async def run():
        c = Connection.from_manifest(manifest())
        r = Registry()
        r.register(c)
        gate = ExecutionGate(
            r,
            rate_limits=RateLimits(global_calls=1) if mode == "rate" else RateLimits(),
        )
        source = FakeSource(c, discovery("a", "b"))
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(CTX, auto_cycle=4 if mode == "cycle" else 1)
            count = {"identical": 3, "same": 4, "total": 7, "cycle": 1, "rate": 2}[mode]
            for i in range(count):
                result = await gate.invoke(
                    c.id,
                    "a" if mode != "total" or i % 2 else "b",
                    {"value": 1 if mode == "identical" else i},
                    loop,
                )
            assert result["outcome"] == "budget_exceeded"
            assert len(source.calls) == count - 1

    asyncio.run(run())


def test_stop_validation_native_resource_and_tasks():
    async def run():
        c = Connection.from_manifest(manifest())
        r = Registry()
        r.register(c)
        data = discovery("normal", "task")
        data.tools[1]["execution"] = {"taskSupport": "required"}
        source = FakeSource(c, data)
        gate = ExecutionGate(r)
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(CTX)
            assert (await gate.invoke(c.id, "normal", {"value": "wrong"}, loop))[
                "error_category"
            ] == "validation"
            assert (await gate.invoke(c.id, "task", {"value": 1}, loop))[
                "outcome"
            ] == "unsupported"
            resource = await gate.read_resource(c.id, "test://resource", loop)
            assert (
                resource["native_payload"]["contents"][0]["text"] == "resource native"
            )
            result = await gate.invoke(c.id, "normal", {"value": 1}, loop)
            assert result["native_payload"] == source.result
            gate.stop(loop)
            assert (await gate.invoke(c.id, "normal", {"value": 2}, loop))[
                "outcome"
            ] == "cancel_requested"
            assert await gate.tasks.status(c.id, "task") == "unsupported"

    asyncio.run(run())


@pytest.mark.parametrize("restricted", ["deny", "require_confirmation"])
def test_restrictions_fail_closed(restricted):
    async def run():
        c = Connection.from_manifest(
            manifest(
                restrictions=[
                    {"target": {"tool_name": "native-tool"}, "action": restricted}
                ]
            )
        )
        r = Registry()
        r.register(c)
        gate = ExecutionGate(r)
        source = FakeSource(c)
        async with gate.attach(c.id, source):
            assert (
                await gate.invoke(
                    c.id, "native-tool", {"value": 1}, gate.begin_loop(CTX)
                )
            )["error_category"] == "policy"
            assert not source.calls

    asyncio.run(run())


def test_mrtr_keeps_original_request_and_one_use_token():
    async def run():
        c = Connection.from_manifest(manifest())
        r = Registry()
        r.register(c)
        gate = ExecutionGate(r)
        source = FakeSource(c)
        source.result = {
            "resultType": "input_required",
            "requestState": "  opaque;not-json  ",
            "inputRequests": {"answer": {"method": "elicitation/create"}},
        }
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(CTX)
            first = await gate.invoke(c.id, "native-tool", {"value": 7}, loop)
            assert first["outcome"] == "input_required"
            token = first["interaction_id"]
            with pytest.raises(MCPFailure):
                await gate.resume(token, {"answer": {}}, gate.begin_loop(CTX))
            source.result = {"content": [{"type": "text", "text": "done"}]}
            final = await gate.resume(token, {"answer": {"action": "accept"}}, loop)
            assert final["outcome"] == "succeeded"
            assert source.calls[-1] == (
                "native-tool",
                {"value": 7},
                {
                    "input_responses": {"answer": {"action": "accept"}},
                    "request_state": "  opaque;not-json  ",
                },
            )
            with pytest.raises(MCPFailure):
                await gate.resume(token, {"answer": {}}, loop)

    asyncio.run(run())


@pytest.mark.parametrize("trusted,maximum", [(False, 1), (True, 2)])
def test_connection_concurrency(trusted, maximum):
    async def run():
        c = Connection.from_manifest(manifest(trusted=trusted))
        r = Registry()
        r.register(c)
        gate = ExecutionGate(r)
        source = FakeSource(c)
        active = peak = 0

        async def call(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return {"content": []}

        source.call_tool = call
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(CTX)
            results = await asyncio.gather(
                *(
                    gate.invoke(c.id, "native-tool", {"value": i}, loop)
                    for i in range(2)
                )
            )
            assert all(r["outcome"] == "succeeded" for r in results)
            assert peak == maximum

    asyncio.run(run())


@pytest.mark.parametrize("policy", ["character", "user", "binding"])
def test_sharing_and_binding_rejection(policy):
    async def run():
        sharing = (
            {"mode": "character_bound", "character_id": "other"}
            if policy == "character"
            else {"mode": "user_bound", "user_id": "owner"}
            if policy == "user"
            else None
        )
        c = Connection.from_manifest(
            manifest(sharing=sharing, binding=policy == "binding")
        )
        r = Registry()
        r.register(c)
        gate = ExecutionGate(r)
        source = FakeSource(c)
        async with gate.attach(c.id, source):
            result = await gate.invoke(
                c.id, "native-tool", {"value": 1}, gate.begin_loop(CTX)
            )
            assert result["error_category"] == "policy"
            assert not source.calls

    asyncio.run(run())


def test_validated_binding_and_confirmation_arguments_are_stable():
    class Binding:
        async def validate(
            self, connection_id, operation, character_id, binding_id, session_id
        ):
            return binding_id == "resolved" and character_id == "miori"

    async def run():
        arguments = {"value": 1}

        class Confirmation:
            async def prepare(self, call, *, live, request_id=None):
                from app.addon_action.models import (
                    ApprovalKey,
                    ApprovalTicket,
                    OperationGroup,
                )

                assert call.arguments == {"value": 1}
                arguments["value"] = 2
                await asyncio.sleep(0)
                live()
                return ApprovalTicket(
                    ApprovalKey(
                        call.connection_id,
                        call.connection_identity,
                        OperationGroup.HIGH_IMPACT,
                        call.scene,
                    )
                )

            async def validate_egress(self, arguments):
                assert arguments == {"value": 1}

            def consume(self, ticket):
                return True

            def end_loop(self, loop_id):
                pass

        c = Connection.from_manifest(
            manifest(
                binding=True,
                restrictions=[
                    {
                        "target": {"tool_name": "native-tool"},
                        "action": "require_confirmation",
                    }
                ],
            )
        )
        r = Registry()
        r.register(c)
        source = FakeSource(c)
        gate = ExecutionGate(r, bindings=Binding(), confirmations=Confirmation())
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(
                ExecutionContext("miori", "s", binding_id="resolved")
            )
            assert (await gate.invoke(c.id, "native-tool", arguments, loop))[
                "outcome"
            ] == "succeeded"
            assert source.calls[0][1] == {"value": 1}

    asyncio.run(run())


def test_mrtr_round_limit_independent_of_normal_budget():
    async def run():
        c = Connection.from_manifest(
            manifest(
                budget={
                    "max_calls_per_loop": 20,
                    "max_consecutive_same_tool": 20,
                    "max_identical_call": 20,
                    "normal_max_auto_cycles": 3,
                }
            )
        )
        r = Registry()
        r.register(c)
        gate = ExecutionGate(r, max_input_rounds=2)
        source = FakeSource(c)
        source.result = {
            "resultType": "input_required",
            "requestState": "opaque",
            "inputRequests": {"a": {"method": "elicitation/create"}},
        }
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(CTX)
            first = await gate.invoke(c.id, "native-tool", {"value": 1}, loop)
            second = await gate.resume(first["interaction_id"], {"a": {}}, loop)
            last = await gate.resume(second["interaction_id"], {"a": {}}, loop)
            assert last["native_error"]["code"] == "input_round_limit"
            assert len(source.calls) == 2

    asyncio.run(run())


def test_unknown_writer_excludes_readers_and_stop_rechecks_waiting_call():
    async def run():
        c = Connection.from_manifest(manifest(trusted=True))
        r = Registry()
        r.register(c)
        data = discovery("read", "write")
        data.tools[1]["annotations"] = {}
        source = FakeSource(c, data)
        gate = ExecutionGate(r)
        entered = asyncio.Event()
        release = asyncio.Event()
        called = []

        async def call(name, arguments, **kwargs):
            called.append(name)
            if name == "read":
                entered.set()
                await release.wait()
            return {"content": []}

        source.call_tool = call
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(CTX)
            read = asyncio.create_task(gate.invoke(c.id, "read", {"value": 1}, loop))
            await entered.wait()
            write = asyncio.create_task(gate.invoke(c.id, "write", {"value": 1}, loop))
            await asyncio.sleep(0)
            assert called == ["read"]
            gate.stop(loop)
            release.set()
            await read
            assert (await write)["outcome"] == "cancel_requested"
            assert called == ["read"]

    asyncio.run(run())


@pytest.mark.parametrize("layer", ["connection", "session"])
def test_rate_limit_layers_and_window_expiry(layer):
    async def run():
        c = Connection.from_manifest(manifest())
        r = Registry()
        r.register(c)
        source = FakeSource(c)
        clock = [0.0]
        rate = (
            RateLimits(connection_calls=1)
            if layer == "connection"
            else RateLimits(session_calls=1)
        )
        gate = ExecutionGate(r, rate_limits=rate, clock=lambda: clock[0])
        async with gate.attach(c.id, source):
            loop = gate.begin_loop(CTX)
            assert (await gate.invoke(c.id, "native-tool", {"value": 1}, loop))[
                "outcome"
            ] == "succeeded"
            assert (await gate.invoke(c.id, "native-tool", {"value": 2}, loop))[
                "outcome"
            ] == "budget_exceeded"
            clock[0] = 61
            assert (await gate.invoke(c.id, "native-tool", {"value": 2}, loop))[
                "outcome"
            ] == "succeeded"

    asyncio.run(run())


def test_sampling_input_required_is_unsupported():
    async def run():
        c = Connection.from_manifest(manifest())
        r = Registry()
        r.register(c)
        source = FakeSource(c)
        gate = ExecutionGate(r)
        source.result = {
            "resultType": "input_required",
            "inputRequests": {"sample": {"method": "sampling/createMessage"}},
        }
        async with gate.attach(c.id, source):
            result = await gate.invoke(
                c.id, "native-tool", {"value": 1}, gate.begin_loop(CTX)
            )
            assert result["outcome"] == "unsupported"
            assert "interaction_id" not in result

    asyncio.run(run())


def test_non_json_arguments_fail_without_invoking():
    async def run():
        c = Connection.from_manifest(manifest())
        registry = Registry()
        registry.register(c)
        source = FakeSource(c)
        gate = ExecutionGate(registry)
        async with gate.attach(c.id, source):
            result = await gate.invoke(
                c.id, "native-tool", {"value": float("nan")}, gate.begin_loop(CTX)
            )
            assert result["error_category"] == "validation"
            assert not source.calls

    asyncio.run(run())


def test_expired_session_rate_buckets_are_reclaimed_without_resetting_live_limits():
    async def run():
        clock = [0.0]
        c = Connection.from_manifest(manifest())
        registry = Registry()
        registry.register(c)
        gate = ExecutionGate(
            registry, rate_limits=RateLimits(session_calls=1), clock=lambda: clock[0]
        )
        async with gate.attach(c.id, FakeSource(c)):
            for index in range(5):
                loop = gate.begin_loop(ExecutionContext("miori", f"session-{index}"))
                assert (await gate.invoke(c.id, "native-tool", {"value": index}, loop))[
                    "outcome"
                ] == "succeeded"
                gate.end_loop(loop)
            loop = gate.begin_loop(ExecutionContext("miori", "session-0"))
            assert (await gate.invoke(c.id, "native-tool", {"value": 8}, loop))[
                "outcome"
            ] == "budget_exceeded"
            gate.end_loop(loop)
            clock[0] = 61.0
            loop = gate.begin_loop(ExecutionContext("miori", "new-session"))
            assert (await gate.invoke(c.id, "native-tool", {"value": 9}, loop))[
                "outcome"
            ] == "succeeded"
            assert set(gate._rates) == {
                ("global",),
                ("connection", c.id),
                ("session", "miori", "new-session"),
            }

    asyncio.run(run())


def test_execution_audit_keeps_character_on_success_and_denial():
    async def run():
        c = Connection.from_manifest(manifest())
        registry = Registry()
        registry.register(c)
        gate = ExecutionGate(registry)
        async with gate.attach(c.id, FakeSource(c)):
            loop = gate.begin_loop(CTX)
            for operation in ["native-tool", "not-granted"]:
                result = await gate.invoke(c.id, operation, {"value": 1}, loop)
                assert result["audit"]["character_id"] == CTX.character_id
            with pytest.raises(MCPFailure, match="unknown_execution_loop"):
                await gate.invoke(c.id, "native-tool", {"value": 1}, "invalid-loop")

    asyncio.run(run())
