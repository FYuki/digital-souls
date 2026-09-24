"""標準MCP Tool/Resourceの結果をEvent履歴契約へ投影する。"""
from __future__ import annotations

import json
from dataclasses import replace
from collections.abc import Callable

from app.external_mcp import ExecutionContext, ExecutionGate
from app.external_mcp.models import Json, encode
from app.tool_use.binding import BindingResolver, apply_binding
from app.tool_use.projection import Sanitizer
from .contracts import Baseline, Operation, Page, Source, failure, parse_baseline, parse_page
from .store import EventStore


class MCPEventReader:
    def __init__(self, gate: ExecutionGate, store: EventStore, sanitizer: Sanitizer) -> None:
        self.gate, self.store, self.sanitizer = gate, store, sanitizer

    def guard(self, source: Source) -> None:
        if not self.store.consumers(source.id):
            raise failure("event_source_unsubscribed")
        self.store.pin_identity(source.id, self.gate.registry.entry(source.connection_id).connection.identity)

    def context(self, source: Source, operation: Operation, context: ExecutionContext) -> ExecutionContext:
        if context.session_id != source.context.session_id:
            raise failure("invalid_event_budget_context")
        if context.binding_id is None:
            return context
        # 設定上の対象IDを毎回解決する。会話内の一時bindingを永続化しない。
        resolver = self.gate.bindings
        if not isinstance(resolver, BindingResolver):
            raise failure("event_binding_resolver_unavailable")
        resolved, constraints = resolver.resolve(
            context.character_id, context.session_id, source.connection_id, operation.ref,
            explicit=context.binding_id, required=True,
        )
        arguments = json.loads(source.arguments_json)
        if encode(apply_binding(arguments, constraints)) != encode(arguments):
            raise failure("binding_argument_mismatch")
        return replace(context, binding_id=resolved)

    async def call(
        self, source: Source, operation: Operation, context: ExecutionContext,
        *, cursor: str | None = None, validate_only: bool = False,
        extra_guard: Callable[[], None] = lambda: None, charge_validation: bool = False,
    ) -> Json:
        arguments = json.loads(source.arguments_json)
        if cursor is not None:
            arguments.update(cursor=cursor, limit=source.limits.page_size)
        if not self.sanitizer.arguments_allowed(arguments):
            raise failure("event_egress_denied")
        def guard() -> None:
            self.guard(source)
            extra_guard()
        native = await self.gate.event_read(
            source.connection_id, operation.ref, operation.kind, operation.definition_digest,
            arguments, self.context(source, operation, context), guard=guard,
            registered_references=True, validate_only=validate_only, resource_page=cursor is not None, charge_validation=charge_validation,
        )
        if validate_only:
            return {}
        # raw本文はログ・DBへ渡さない。対話入力要求もバックグラウンドでは扱わない。
        if native.get("isError") or "inputRequests" in native:
            raise failure("event_result_unavailable")
        try:
            if len(encode(native).encode()) > 4 * 1024 * 1024:
                raise failure("event_result_too_large")
            if operation.kind == "tool":
                result = native["structuredContent"]
            else:
                parts = native["contents"]
                if not isinstance(parts, list) or len(parts) != 1:
                    raise ValueError()
                result = json.loads(parts[0]["text"])
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, KeyError, TypeError, RecursionError):
            raise failure("invalid_event_result") from None

    async def authorize(
        self, source: Source, context: ExecutionContext,
        *, guard: Callable[[], None] = lambda: None, delivery: bool = False,
    ) -> None:
        for operation in (source.history, source.snapshot):
            # buffered deliveryにも現在の設定・定義・egressを適用する。
            await self.call(source, operation, context, validate_only=True, extra_guard=guard,
                            charge_validation=delivery and operation == source.history)

    async def snapshot(self, source: Source, context: ExecutionContext) -> Baseline:
        return parse_baseline(await self.call(source, source.snapshot, context), self.sanitizer)

    async def history(
        self, source: Source, context: ExecutionContext, *, cursor: str, after: int, epoch: str,
    ) -> Page:
        return parse_page(
            await self.call(source, source.history, context, cursor=cursor),
            after=after, epoch=epoch, sanitizer=self.sanitizer, limits=source.limits,
        )
