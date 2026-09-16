"""通知と独立Core参照に共通の、会話・LLM非依存の有限取得。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Literal

from app.addon_events.runtime import EventRuntime
from app.addon_events.contracts import Source
from app.addon_action.models import ExecutionScene
from app.external_mcp import ExecutionContext, ExecutionGate
from app.external_mcp.models import Json, MCPFailure, digest, encode
from app.tool_use.binding import BindingResolver, apply_binding
from app.tool_use.projection import Sanitizer
from .contracts import REFERENCE_KEYS, Reference, Registration, failure, metadata, safe_token
from .store import NotificationStore


class NotificationReader:
    def __init__(self, gate: ExecutionGate, events: EventRuntime | None, store: NotificationStore,
                 sanitizer: Sanitizer, *, character_exists: Callable[[str], bool]) -> None:
        self.gate, self.events, self.store, self.sanitizer = gate, events, store, sanitizer
        self.character_exists = character_exists
        self.registrations: dict[str, Registration] = {}

    def registration(self, reference: Reference) -> Registration:
        registration = self.registrations.get(reference.registration_id)
        if registration is None or registration.signature != reference.registration_signature:
            raise failure("notification_reference_unavailable")
        return registration

    def source(self, registration: Registration) -> Source:
        if self.events is None or registration.source_id not in self.events.sources:
            raise failure("notification_source_unavailable")
        return self.events.sources[registration.source_id]

    def _context(self, registration: Registration, arguments: Json) -> tuple[ExecutionContext, str]:
        source = self.source(registration)
        context = ExecutionContext(registration.character_id, f"notification:{registration.id}", registration.auth_user_id, scene=ExecutionScene.AUTONOMOUS)
        binding_signature = digest(None)
        if registration.binding_id is not None:
            resolver = self.gate.bindings
            if not isinstance(resolver, BindingResolver):
                raise failure("notification_binding_unavailable")
            target = next((target for target in resolver.candidates(registration.character_id, source.connection_id, registration.detail.ref)
                           if target.id == registration.binding_id), None)
            if target is None:
                raise failure("notification_binding_unavailable")
            resolved, constraints = resolver.resolve(registration.character_id, context.session_id,
                source.connection_id, registration.detail.ref, explicit=registration.binding_id, required=True)
            if encode(apply_binding(arguments, constraints)) != encode(arguments):
                raise failure("notification_binding_mismatch")
            binding_signature = digest([target.id, target.connection_id, target.character_id, target.operations, target.arguments_json])
            context = replace(context, binding_id=resolved)
        return context, binding_signature

    def guard(self, registration: Registration, arguments: Json, binding_signature: str) -> None:
        if self.registrations.get(registration.id) != registration or not self.character_exists(registration.character_id):
            raise failure("notification_registration_unavailable")
        source = self.source(registration)
        _, current_binding = self._context(registration, arguments)
        if binding_signature != current_binding:
            raise failure("notification_binding_changed")
        identity = digest([self.gate.registry.entry(source.connection_id).connection.identity, source.signature])
        self.store.pin(registration, identity, binding_signature)
        for token in (registration.id, registration.source_id, registration.character_id, registration.event_type):
            if self.sanitizer.text(token) != token:
                raise failure("notification_private_reference")

    def arguments(self, registration: Registration, value: Json | None = None) -> Json:
        arguments: Json = json.loads(registration.arguments_json)
        if value is not None:
            value = metadata(value, self.sanitizer)
            if value["type"] != registration.event_type or any(value.get(k) != v for k, v in json.loads(registration.match_json).items()):
                raise failure("notification_reference_mismatch")
            if registration.kind == "result" and "revision" not in value:
                raise failure("notification_revision_required")
            for key, reference_key in json.loads(registration.reference_arguments_json).items():
                if reference_key not in value:
                    raise failure("notification_reference_missing")
                arguments[key] = value[reference_key]
        if not self.sanitizer.arguments_allowed(arguments):
            raise failure("notification_egress_denied")
        return arguments

    async def authorize(self, registration: Registration, *, viewer: str | None = None, value: Json | None = None) -> None:
        if viewer is not None and viewer not in registration.recipients:
            raise failure("notification_permission_denied")
        arguments = self.arguments(registration, value)
        context, binding_signature = self._context(registration, arguments)
        source = self.source(registration)
        def guard() -> None:
            self.guard(registration, arguments, binding_signature)
        operation = registration.detail
        # 接続の認証主体と閲覧者の現在grantを別々に検証する。
        for current in (context,) if viewer is None else (context, replace(context, user_id=viewer)):
            await self.gate.event_read(source.connection_id, operation.ref, operation.kind,
                operation.definition_digest, arguments, current, guard=guard, validate_only=True, registered_references=True)

    def reference(self, registration_id: str, event_key: str, value: Json) -> Reference:
        """登録済みの依頼側consumerが保持する参照を発行する。公開APIには露出しない。"""
        registration = self.registrations.get(registration_id)
        if registration is None:
            raise failure("notification_registration_unavailable")
        projected = metadata(value, self.sanitizer)
        self.arguments(registration, projected)
        return Reference(registration.id, registration.signature, event_key, encode(projected))

    async def read(self, reference: Reference, viewer: str, *,
                   purpose: Literal["notification_detail", "registered_consumer"] = "notification_detail",
                   caller_character_id: str | None = None) -> Json:
        try:
            async with asyncio.timeout(self.store.limits.read_seconds):
                registration = self.registration(reference)
                if purpose not in {"notification_detail", "registered_consumer"} or (
                    purpose == "registered_consumer" and caller_character_id != registration.character_id
                ) or (caller_character_id is not None and caller_character_id != registration.character_id):
                    return {"state": "permission_denied"}
                value = metadata(reference.metadata, self.sanitizer)
                await self.authorize(registration, viewer=viewer, value=value)
                self.store.charge_read(registration)
                arguments = self.arguments(registration, value)
                context, binding_signature = self._context(registration, arguments)
                source = self.source(registration)
                native = await self.gate.event_read(source.connection_id, registration.detail.ref,
                    registration.detail.kind, registration.detail.definition_digest, arguments, context,
                    guard=lambda: self.guard(registration, arguments, binding_signature), registered_references=True)
                await self.authorize(registration, viewer=viewer, value=value)
                if native.get("isError") or "inputRequests" in native or len(encode(native).encode()) > 4 * 1024 * 1024:
                    return {"state": "unavailable"}
                if registration.detail.kind == "tool":
                    result = native.get("structuredContent")
                else:
                    parts = native.get("contents")
                    if not isinstance(parts, list) or len(parts) != 1:
                        return {"state": "unavailable"}
                    result = json.loads(parts[0]["text"])
                if not isinstance(result, dict):
                    return {"state": "unavailable"}
                state = result.get("state")
                if state in ("expired", "deleted", "not_found", "unavailable", "permission_denied"):
                    return {"state": state}
                if state != "available":
                    return {"state": "unavailable"}
                # 提供元は内容と一緒に対象参照を返す。URL追従や別実行への置換はしない。
                actual = result.get("references")
                if not isinstance(actual, dict):
                    return {"state": "unavailable"}
                required = {k: v for k, v in value.items() if k in REFERENCE_KEYS and k != "revision"}
                if registration.kind == "result":
                    required["revision"] = value["revision"]
                if any(str(actual.get(k)) != str(v) for k, v in required.items()):
                    return {"state": "revision_mismatch"}
                content = result.get("text")
                if not isinstance(content, str):
                    return {"state": "unavailable"}
                text = self.sanitizer.text(content)
                encoded = text.encode()
                omitted = len(encoded) > self.store.limits.read_bytes or len(content) > 16_384
                text = encoded[:self.store.limits.read_bytes].decode(errors="ignore")
                revision = safe_token(str(actual["revision"]), self.sanitizer) if "revision" in actual else None
                return {"state": "available", "text": text, "omitted": omitted,
                        "acquired_at": self.store.clock(), "revision": revision, "untrusted": True,
                        "purpose": purpose,
                        "kind": registration.kind, "source_id": registration.source_id,
                        "character_id": registration.character_id}
        except MCPFailure as error:
            if "budget" in error.code or "rate_limit" in error.code:
                return {"state": "budget_exceeded"}
            if "confirmation" in error.code:
                return {"state": "confirmation_required"}
            if error.category in {"policy", "authentication"} or any(word in error.code for word in ("permission", "binding", "registration", "private")):
                return {"state": "permission_denied"}
            return {"state": "unavailable"}
        except (TimeoutError, ValueError, KeyError, TypeError, RecursionError):
            return {"state": "unavailable"}
