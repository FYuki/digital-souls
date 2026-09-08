"""選択済みのnative operationにgrant/budget/policyを一括適用する。"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable
from uuid import uuid4

from .models import (
    Json,
    MCPFailure,
    Snapshot,
    digest,
    encode,
    now,
    validate_arguments,
    validate_contract,
)
from .ports import (
    BindingValidatorPort,
    CapabilitySource,
    ConfirmationPolicyPort,
    UnsupportedTasks,
)
from .registry import Registry


@dataclass(frozen=True)
class ExecutionContext:
    character_id: str
    session_id: str
    user_id: str | None = None
    binding_id: str | None = None


@dataclass
class _Loop:
    context: ExecutionContext
    snapshots: dict[str, tuple[Snapshot, int]]
    cycle: int
    calls: int = 0
    last: tuple[str, str] | None = None
    consecutive: int = 0
    identical: Counter[str] = field(default_factory=Counter)
    stopped: bool = False


@dataclass(frozen=True)
class _Pending:
    loop_id: str
    connection_id: str
    operation: str
    kind: str
    arguments_json: str
    request_state: str | None
    request_ids: frozenset[str]
    rounds: int
    binding_id: str | None


class _ConnectionLock:
    """writer待機中の新規readerを止め、unknownとreadの重複も防ぐ。"""

    def __init__(self, parallelism: int) -> None:
        self.condition = asyncio.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0
        self.parallelism = parallelism

    @asynccontextmanager
    async def hold(self, parallel: bool) -> AsyncIterator[None]:
        async with self.condition:
            if parallel:
                await self.condition.wait_for(
                    lambda: (
                        not self.writer
                        and not self.waiting_writers
                        and self.readers < self.parallelism
                    )
                )
                self.readers += 1
            else:
                self.waiting_writers += 1
                try:
                    await self.condition.wait_for(
                        lambda: not self.writer and not self.readers
                    )
                    self.writer = True
                finally:
                    self.waiting_writers -= 1
                    self.condition.notify_all()
        try:
            yield
        finally:
            async with self.condition:
                if parallel:
                    self.readers -= 1
                else:
                    self.writer = False
                self.condition.notify_all()


@dataclass(frozen=True)
class RateLimits:
    global_calls: int = 120
    connection_calls: int = 60
    session_calls: int = 30
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        if (
            min(
                self.global_calls,
                self.connection_calls,
                self.session_calls,
                self.window_seconds,
            )
            <= 0
        ):
            raise ValueError("rate limits must be positive")


class ExecutionGate:
    def __init__(
        self,
        registry: Registry,
        *,
        bindings: BindingValidatorPort | None = None,
        confirmations: ConfirmationPolicyPort | None = None,
        rate_limits: RateLimits = RateLimits(),
        clock: Callable[[], float] = time.monotonic,
        parallelism: int = 4,
        max_input_rounds: int = 3,
    ) -> None:
        if parallelism < 1 or max_input_rounds < 1:
            raise ValueError("limits must be positive")
        self.registry = registry
        self.bindings = bindings
        self.confirmations = confirmations
        self.tasks = UnsupportedTasks()
        self.rate_limits = rate_limits
        self.clock = clock
        self.parallelism = parallelism
        self.max_input_rounds = max_input_rounds
        self._sources: dict[str, CapabilitySource] = {}
        self._locks: dict[str, _ConnectionLock] = {}
        self._loops: dict[str, _Loop] = {}
        self._pending: dict[str, _Pending] = {}
        self._rates: dict[tuple[str, ...], deque[float]] = {}
        self._last_rate_cleanup = float("-inf")

    @asynccontextmanager
    async def attach(
        self, connection_id: str, source: CapabilitySource
    ) -> AsyncIterator[None]:
        """接続を所有するcontextの内側で使用し、終了時にはofflineへ戻す。"""
        entry = self.registry.entry(connection_id)
        if connection_id in self._sources or not source.connected:
            raise MCPFailure("policy", "invalid_attachment")
        # Adapterが設定を公開する場合は登録済みidentityと照合する。
        connection = getattr(source, "connection", entry.connection)
        if connection.id != connection_id:
            raise MCPFailure("policy", "connection_identity_mismatch")
        self.registry.connected(connection)
        self._sources[connection_id] = source
        self._locks.setdefault(connection_id, _ConnectionLock(self.parallelism))
        try:
            await self.refresh(connection_id)
            yield
        finally:
            self.registry.availability(connection_id, "unavailable")
            self._sources.pop(connection_id, None)

    async def refresh(self, connection_id: str) -> Snapshot:
        entry = self.registry.entry(connection_id)
        source = self._sources.get(connection_id)
        if not entry.linked or source is None or not source.connected:
            raise MCPFailure("unavailable", "not_connected")
        generation = entry.generation
        # discoveryは読取専用。実行との競合を避け、stagedだけを更新する。
        async with self._locks[connection_id].hold(False):
            for attempt in range(2):
                try:
                    discovery = await source.discover()
                    if entry.generation != generation:
                        raise MCPFailure("policy", "connection_changed")
                    return self.registry.stage(connection_id, discovery)
                except MCPFailure as error:
                    if attempt == 0 and error.retryable:
                        continue
                    self.registry.operation_failure(connection_id, error)
                    raise
        raise AssertionError("unreachable")

    def begin_loop(self, context: ExecutionContext, *, auto_cycle: int = 1) -> str:
        if not context.character_id or not context.session_id or auto_cycle < 1:
            raise MCPFailure("validation", "invalid_context")
        token = str(uuid4())
        self._loops[token] = _Loop(context, self.registry.activate(), auto_cycle)
        return token

    def stop(self, loop_id: str) -> None:
        self._loop(loop_id).stopped = True

    def catalog_snapshots(self, loop_id: str) -> dict[str, Snapshot]:
        """現在loopの利用可能な正本だけを返す。stagedをactivateしない。"""
        loop = self._loop(loop_id)
        result: dict[str, Snapshot] = {}
        for connection_id, (snapshot, generation) in loop.snapshots.items():
            try:
                self._live(loop, connection_id, generation)
                self._sharing(loop, connection_id)
            except MCPFailure:
                continue
            result[connection_id] = snapshot
        return result

    def _sharing(self, loop: _Loop, connection_id: str) -> None:
        sharing = self.registry.entry(connection_id).connection.manifest["core_policy"][
            "sharing"
        ]
        if (
            sharing["mode"] == "character_bound"
            and sharing["character_id"] != loop.context.character_id
        ) or (
            sharing["mode"] == "user_bound"
            and sharing["user_id"] != loop.context.user_id
        ):
            raise MCPFailure("policy", "sharing_denied")

    async def refresh_for_loop(self, connection_id: str, loop_id: str) -> Snapshot:
        """会話からのrefreshも同じgrant・停止・予算で制限する。"""
        loop = self._loop(loop_id)
        if connection_id not in loop.snapshots:
            raise MCPFailure("policy", "snapshot_not_granted")
        _, generation = loop.snapshots[connection_id]
        self._sharing(loop, connection_id)
        async with self._locks[connection_id].hold(False):
            for attempt in range(2):
                source = self._live(loop, connection_id, generation)
                self._charge(loop, connection_id, "capability_refresh", {})
                try:
                    discovery = await source.discover()
                    self._live(loop, connection_id, generation)
                    return self.registry.stage(connection_id, discovery)
                except MCPFailure as error:
                    if attempt == 0 and error.retryable:
                        continue
                    self.registry.operation_failure(connection_id, error)
                    raise
        raise AssertionError("unreachable")

    def invalidate_connection(self, connection_id: str) -> None:
        """設定OFFで回答待ちを破棄する。送信済み処理は取り消さない。"""
        self._pending = {
            key: pending
            for key, pending in self._pending.items()
            if pending.connection_id != connection_id
        }

    def end_loop(self, loop_id: str) -> None:
        self.stop(loop_id)
        self._loops.pop(loop_id)
        self._pending = {k: v for k, v in self._pending.items() if v.loop_id != loop_id}

    def _loop(self, loop_id: str) -> _Loop:
        try:
            return self._loops[loop_id]
        except KeyError:
            raise MCPFailure("policy", "unknown_execution_loop") from None

    def _charge(
        self, loop: _Loop, connection_id: str, operation: str, arguments: Json
    ) -> None:
        limits = self.registry.entry(connection_id).connection.manifest["core_policy"][
            "execution_budget"
        ]
        key = (connection_id, operation)
        identical = digest([*key, arguments])
        consecutive = loop.consecutive + 1 if loop.last == key else 1
        if (
            loop.calls >= limits["max_calls_per_loop"]
            or consecutive > limits["max_consecutive_same_tool"]
            or loop.identical[identical] >= limits["max_identical_call"]
            or loop.cycle > limits["normal_max_auto_cycles"]
        ):
            raise MCPFailure("policy", "budget_exceeded")
        rate = self.rate_limits
        checks = [
            (("global",), rate.global_calls),
            (("connection", connection_id), rate.connection_calls),
            (
                ("session", loop.context.character_id, loop.context.session_id),
                rate.session_calls,
            ),
        ]
        current = self.clock()
        if current - self._last_rate_cleanup >= rate.window_seconds:
            # 再利用されないsessionも窓外で回収する。loop終了直後には消さない。
            for stale_key, bucket in list(self._rates.items()):
                while bucket and bucket[0] <= current - rate.window_seconds:
                    bucket.popleft()
                if not bucket:
                    del self._rates[stale_key]
            self._last_rate_cleanup = current
        for key_rate, maximum in checks:
            bucket = self._rates.setdefault(key_rate, deque())
            while bucket and bucket[0] <= current - rate.window_seconds:
                bucket.popleft()
            if len(bucket) >= maximum:
                raise MCPFailure("policy", "rate_limit_exceeded")
        for key_rate, _ in checks:
            self._rates[key_rate].append(current)
        loop.calls += 1
        loop.last, loop.consecutive = key, consecutive
        loop.identical[identical] += 1

    def _live(
        self, loop: _Loop, connection_id: str, generation: int
    ) -> CapabilitySource:
        if loop.stopped:
            raise MCPFailure("policy", "user_stopped")
        entry = self.registry.entry(connection_id)
        source = self._sources.get(connection_id)
        if (
            not entry.linked
            or entry.generation != generation
            or entry.availability not in {"available", "degraded"}
            or source is None
            or not source.connected
        ):
            raise MCPFailure("unavailable", "not_connected")
        if not entry.desired_enabled:
            raise MCPFailure("policy", "connection_disabled")
        return source

    async def invoke(
        self,
        connection_id: str,
        tool_ref: str,
        arguments: Json,
        loop_id: str,
        *,
        binding_id: str | None = None,
    ) -> Json:
        return await self._execute(
            connection_id, tool_ref, arguments, loop_id, "tool", binding_id=binding_id
        )

    async def read_resource(
        self,
        connection_id: str,
        resource_ref: str,
        loop_id: str,
        *,
        binding_id: str | None = None,
    ) -> Json:
        return await self._execute(
            connection_id, resource_ref, {}, loop_id, "resource", binding_id=binding_id
        )

    async def resume(
        self, interaction_id: str, input_responses: Json, loop_id: str
    ) -> Json:
        pending = self._pending.get(interaction_id)
        if pending is None or pending.loop_id != loop_id:
            raise MCPFailure("policy", "invalid_interaction")
        if set(input_responses) != pending.request_ids:
            raise MCPFailure("validation", "invalid_input_responses")
        self._pending.pop(interaction_id)
        return await self._execute(
            pending.connection_id,
            pending.operation,
            json.loads(pending.arguments_json),
            loop_id,
            pending.kind,
            pending=pending,
            responses=input_responses,
            binding_id=pending.binding_id,
        )

    async def _execute(
        self,
        connection_id: str,
        operation: str,
        arguments: Json,
        loop_id: str,
        kind: str,
        *,
        pending: _Pending | None = None,
        responses: Json | None = None,
        binding_id: str | None = None,
    ) -> Json:
        # 不明なloopには監査主体が無いため、envelopeを捏造せず呼出しを拒否する。
        loop = self._loop(loop_id)
        # 明示的な呼出しbindingを優先する。MRTRには解決済み値を固定する。
        binding_id = binding_id if binding_id is not None else loop.context.binding_id
        result: Json = {
            "execution_id": str(uuid4()),
            "connection_instance_id": connection_id,
            "definition_revision": "unresolved",
            "operation_ref": {
                "tool_name" if kind == "tool" else "resource_uri": operation
            },
            "started_at": now(),
            "finished_at": None,
            "outcome": "failed",
            "retry_count": 0,
            "native_payload": None,
            "audit": {"character_id": loop.context.character_id},
        }
        try:
            try:
                arguments = json.loads(encode(arguments))
                responses = (
                    json.loads(encode(responses)) if responses is not None else None
                )
            except (ValueError, TypeError):
                raise MCPFailure("validation", "invalid_arguments") from None
            if connection_id not in loop.snapshots:
                raise MCPFailure("policy", "snapshot_not_granted")
            snapshot, generation = loop.snapshots[connection_id]
            self._live(loop, connection_id, generation)
            connection = self.registry.entry(connection_id).connection
            policy = connection.manifest["core_policy"]
            self._sharing(loop, connection_id)
            if policy["resource_binding_required"] or binding_id is not None:
                if self.bindings is None or not await self.bindings.validate(
                    connection_id,
                    operation,
                    loop.context.character_id,
                    binding_id,
                    loop.context.session_id,
                ):
                    raise MCPFailure("policy", "binding_denied")
            native = snapshot.document
            if kind == "tool":
                tool = next(
                    (t for t in native["tools"] if t["name"] == operation), None
                )
                if tool is None:
                    raise MCPFailure("policy", "operation_not_granted")
                result["definition_revision"] = (
                    f"{connection_id}:{operation}:{tool['schema_digest']}"
                )
                if tool["status"] == "unsupported":
                    raise MCPFailure("policy", "tasks_unsupported")
                validate_arguments(tool["input_schema"], arguments)
                effective = tool["effective_policy"]
            else:
                resource = next(
                    (r for r in native["resources"] if r["uri"] == operation), None
                )
                if resource is None:
                    raise MCPFailure("policy", "operation_not_granted")
                result["definition_revision"] = (
                    f"{connection_id}:{operation}:{digest(resource)}"
                )
                effective = {
                    "effect": "read",
                    "concurrency": "parallel",
                    "retry": "read_once",
                }
            rules = connection.restrictions(operation)
            if "deny" in rules:
                raise MCPFailure("policy", "operation_denied")
            if "require_confirmation" in rules:
                if self.confirmations is None or not await self.confirmations.confirmed(
                    connection_id, operation, arguments, loop.context.character_id
                ):
                    raise MCPFailure("policy", "confirmation_required")
            if pending and pending.rounds >= self.max_input_rounds:
                raise MCPFailure("policy", "input_round_limit")
            parallel = (
                effective["concurrency"] == "parallel" and "force_serial" not in rules
            )
            retry = effective["retry"] == "read_once" and "disable_retry" not in rules
            async with self._locks[connection_id].hold(parallel):
                payload: Json
                for attempt in range(2 if retry else 1):
                    source = self._live(loop, connection_id, generation)
                    if policy["resource_binding_required"] or binding_id is not None:
                        if self.bindings is None or not await self.bindings.validate(
                            connection_id,
                            operation,
                            loop.context.character_id,
                            binding_id,
                            loop.context.session_id,
                        ):
                            raise MCPFailure("policy", "binding_denied")
                    # 非同期validatorを待つ間のstop/relinkもdispatch前に確認する。
                    self._live(loop, connection_id, generation)
                    entry = self.registry.entry(connection_id)
                    if entry.availability == "degraded" and (
                        effective["effect"] != "read"
                        or f"{kind}:{operation}" not in entry.healthy_operations
                    ):
                        raise MCPFailure("policy", "partial_failure_operation_denied")
                    self._charge(loop, connection_id, operation, arguments)
                    try:
                        if kind == "tool":
                            # dispatch直前に元schemaを再検証する。
                            assert tool is not None
                            validate_arguments(tool["input_schema"], arguments)
                            payload = await source.call_tool(
                                operation,
                                arguments,
                                input_responses=responses,
                                request_state=pending.request_state
                                if pending
                                else None,
                            )
                        else:
                            payload = await source.read_resource(
                                operation,
                                input_responses=responses,
                                request_state=pending.request_state
                                if pending
                                else None,
                            )
                        break
                    except MCPFailure as error:
                        if retry and attempt == 0 and error.retryable:
                            result["retry_count"] = 1
                            continue
                        if error.category in {"transport", "protocol", "auth"}:
                            self.registry.operation_failure(connection_id, error)
                        raise
                # 成功後の結果統合はretry区間の外で行う。
                result["native_payload"] = payload
                if payload.get("resultType") == "input_required":
                    self._live(loop, connection_id, generation)
                    request_state = payload.get("requestState")
                    requests = payload.get("inputRequests") or {}
                    if (
                        request_state is not None and not isinstance(request_state, str)
                    ) or not isinstance(requests, dict):
                        raise MCPFailure("protocol", "invalid_input_required")
                    if any(
                        not isinstance(request, dict)
                        or request.get("method") != "elicitation/create"
                        for request in requests.values()
                    ):
                        raise MCPFailure("policy", "required_capability_unsupported")
                    interaction_id = str(uuid4())
                    self._pending[interaction_id] = _Pending(
                        loop_id,
                        connection_id,
                        operation,
                        kind,
                        encode(arguments),
                        request_state,
                        frozenset(requests),
                        (pending.rounds if pending else 0) + 1,
                        binding_id,
                    )
                    result.update(
                        outcome="input_required", interaction_id=interaction_id
                    )
                elif payload.get("isError"):
                    result.update(outcome="failed", error_category="tool_error")
                else:
                    result["outcome"] = "succeeded"
        except MCPFailure as error:
            outcome = "failed"
            if error.code in {"budget_exceeded", "rate_limit_exceeded"}:
                outcome = "budget_exceeded"
            elif error.code in {
                "tasks_unsupported",
                "unsupported_auth",
                "required_capability_unsupported",
            }:
                outcome = "unsupported"
            elif error.code == "user_stopped":
                outcome = "cancel_requested"
            result.update(
                outcome=outcome,
                error_category=error.category,
                native_error={"code": error.code},
            )
        result["finished_at"] = now()
        validate_contract("execution-envelope", result)
        return result
