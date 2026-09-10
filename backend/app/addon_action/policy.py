"""確認キューと呼出元の待機を分離するConfirmationPolicyPort実装。"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.external_mcp.models import Json, MCPFailure, ConfirmationNeeded, digest
from app.tool_use.projection import Sanitizer, bounded_json
import json

from .impact import evaluate_impact
from .models import (
    ActionInvocation,
    ApprovalChoice,
    ApprovalKey,
    ApprovalTicket,
    ExecutionScene,
    Permission,
    OperationGroup,
)
from .store import ActionStore, ConfirmationRequest


class ActionPolicy:
    def __init__(
        self,
        store: ActionStore,
        sanitizer: Sanitizer,
        *,
        egress: Callable[[Json], Awaitable[bool]],
        protected_roots: tuple[Path, ...] = (),
        autonomous_wait_seconds: float = 60,
        conversation_wait_seconds: float = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if any(
            not math.isfinite(v) or v <= 0
            for v in (autonomous_wait_seconds, conversation_wait_seconds)
        ):
            raise ValueError("invalid action confirmation wait")
        self.store, self.sanitizer, self.egress = store, sanitizer, egress
        self.protected_roots = tuple(p.resolve() for p in protected_roots)
        self.autonomous_wait_seconds = autonomous_wait_seconds
        self.conversation_wait_seconds = conversation_wait_seconds
        self.clock = clock

    async def validate_egress(self, arguments: Json) -> None:
        if not self.sanitizer.arguments_allowed(arguments) or not await self.egress(
            arguments
        ):
            raise MCPFailure("policy", "egress_privacy_blocked")

    async def prepare(
        self,
        call: ActionInvocation,
        *,
        live: Callable[[], None],
        request_id: str | None = None,
    ) -> ApprovalTicket:
        outgoing = (
            {"arguments": call.arguments, "input_responses": call.input_responses}
            if call.input_responses is not None
            else call.arguments
        )
        await self.validate_egress(outgoing)
        live()
        impact = evaluate_impact(
            call.static_impact,
            call.arguments,
            binding_id=call.binding_id,
            protected_roots=self.protected_roots,
            force_confirmation=call.force_confirmation,
        )
        if impact.blocked:
            raise MCPFailure("policy", impact.reason)
        if call.input_responses:
            # 追加回答は通常引数schemaの保証範囲外。実値の保護境界も検証する。
            additional = evaluate_impact(
                {"effect": "unknown"},
                call.input_responses,
                protected_roots=self.protected_roots,
            )
            if additional.blocked:
                raise MCPFailure("policy", additional.reason)
            if impact.group != OperationGroup.HIGH_IMPACT:
                impact = additional
        key = ApprovalKey(
            call.connection_id, call.connection_identity, impact.group, call.scene
        )
        fingerprint = digest(
            [call.connection_id, call.connection_identity, impact.group, call.scene,
             call.operation, call.binding_id, outgoing]
        )
        request: ConfirmationRequest | None = None
        if request_id is not None:
            request = self.store.request(request_id)
            if (
                request.key != key
                or request.loop_id != call.loop_id
                or request.character_id != call.character_id
                or request.session_id != call.session_id
                or request.fingerprint != fingerprint
            ):
                raise MCPFailure("policy", "confirmation_mismatch")
            if not request.waiting or self.clock() >= request.wait_until:
                self.store.end_wait(request.id)
                raise MCPFailure("policy", "confirmation_wait_ended")
            if request.choice == ApprovalChoice.REJECT:
                raise MCPFailure("policy", "action_rejected")
        state = self.store.state(key)
        if state.permission == Permission.DENIED:
            raise MCPFailure("policy", "action_rejected")
        if state.allowed:
            return ApprovalTicket(key, request_id)
        if request is None:
            visible = {
                k: v
                for k, v in call.arguments.items()
                if k not in call.core_argument_keys
            }
            preview_outgoing = (
                {"arguments": visible, "input_responses": call.input_responses}
                if call.input_responses is not None
                else visible
            )
            preview = {
                "connection": self.sanitizer.text(call.connection_label, maximum=128),
                "operation": self.sanitizer.text(call.operation, maximum=128),
                "target": self.sanitizer.text(call.binding_id or "呼び出し引数で指定"),
                "arguments": json.loads(
                    bounded_json(self.sanitizer.value(preview_outgoing), 2048)
                ),
                "reason": impact.reason,
            }
            request = self.store.enqueue(
                key,
                character_id=call.character_id,
                session_id=call.session_id,
                loop_id=call.loop_id,
                fingerprint=fingerprint,
                preview=preview,
                wait_seconds=self.autonomous_wait_seconds
                if call.scene == ExecutionScene.AUTONOMOUS
                else self.conversation_wait_seconds,
                now=self.clock(),
            )
        if not request.waiting or self.clock() >= request.wait_until:
            self.store.end_wait(request.id)
            raise MCPFailure("policy", "confirmation_wait_ended")
        if request.choice == ApprovalChoice.REJECT:
            raise MCPFailure("policy", "action_rejected")
        if call.scene == ExecutionScene.CONVERSATION:
            raise ConfirmationNeeded(request.id)
        try:
            while True:
                live()
                current = self.store.request(request.id)
                # 承認とtimeoutが同時でも待機期限を過ぎた元操作は実行しない。
                if not current.waiting or self.clock() >= current.wait_until:
                    self.store.end_wait(request.id)
                    raise MCPFailure("policy", "confirmation_wait_ended")
                if current.choice == ApprovalChoice.REJECT:
                    raise MCPFailure("policy", "action_rejected")
                if current.choice is not None:
                    return ApprovalTicket(key, request.id)
                await asyncio.sleep(min(0.1, max(0, current.wait_until - self.clock())))
        except BaseException:
            self.store.end_wait(request.id)
            raise

    def consume(self, ticket: ApprovalTicket) -> bool:
        return self.store.consume_request(ticket.key, ticket.request_id, self.clock())

    def end_loop(self, loop_id: str) -> None:
        self.store.end_loop(loop_id)

    def end_wait(self, request_id: str) -> None:
        self.store.end_wait(request_id)
