"""Core登録情報と、外部本文を含まない通知契約。"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from app.addon_events.contracts import Operation, token
from app.external_mcp.models import Json, MCPFailure, digest, encode
from app.tool_use.projection import Sanitizer

Decision = Literal["ignore", "state-only", "notify"]
ReadState = Literal["available", "expired", "deleted", "not_found", "unavailable", "permission_denied", "revision_mismatch", "confirmation_required", "budget_exceeded"]
REFERENCE_KEYS = frozenset({"resource_ref", "task_ref", "rule_ref", "execution_ref", "revision"})
RESULT_KINDS = frozenset({"completed", "failed", "attention", "updated"})


def failure(code: str) -> MCPFailure:
    return MCPFailure("notification", code)


def safe_token(value: Any, sanitizer: Sanitizer | None = None) -> str:
    try:
        return token(value, sanitizer)
    except MCPFailure:
        raise failure("invalid_notification_reference") from None


@dataclass(frozen=True)
class Limits:
    retention_seconds: float = 30 * 86400
    max_per_user: int = 10_000
    poll_seconds: float = 5.0
    read_seconds: float = 15.0
    read_bytes: int = 16_384
    scope_reads_per_minute: int = 12
    character_reads_per_minute: int = 60

    def __post_init__(self) -> None:
        for value in (self.retention_seconds, self.poll_seconds, self.read_seconds):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise failure("invalid_notification_limits")
        if self.retention_seconds > 30 * 86400 or self.read_seconds > 60:
            raise failure("invalid_notification_limits")
        for value in (self.max_per_user, self.read_bytes, self.scope_reads_per_minute, self.character_reads_per_minute):
            if type(value) is not int or value < 1:
                raise failure("invalid_notification_limits")
        if self.max_per_user > 100_000 or not 256 <= self.read_bytes <= 65_536:
            raise failure("invalid_notification_limits")


@dataclass(frozen=True)
class Registration:
    id: str
    source_id: str
    character_id: str
    event_type: str
    detail: Operation
    owner_user_id: str = "local"
    auth_user_id: str | None = None
    viewers: tuple[str, ...] = ()
    binding_id: str | None = None
    kind: Literal["monitor", "result"] = "monitor"
    decision: Decision = "ignore"
    match_json: str = field(default="{}", repr=False)
    arguments_json: str = field(default="{}", repr=False)
    reference_arguments_json: str = field(default="{}", repr=False)
    origin_conversation: str | None = None
    origin_message: str | None = None
    budget_scope: str | None = None

    def __post_init__(self) -> None:
        for value in (self.id, self.source_id, self.character_id, self.event_type, self.owner_user_id, *self.viewers):
            safe_token(value)
        for optional_value in (self.auth_user_id, self.binding_id, self.origin_conversation, self.origin_message, self.budget_scope):
            if optional_value is not None:
                safe_token(optional_value)
        if self.kind not in {"monitor", "result"} or self.decision not in {"ignore", "state-only", "notify"}:
            raise failure("invalid_notification_registration")
        match, arguments, references = (json.loads(v) for v in (self.match_json, self.arguments_json, self.reference_arguments_json))
        if not all(isinstance(v, dict) for v in (match, arguments, references)):
            raise failure("invalid_notification_registration")
        if set(match) - REFERENCE_KEYS or set(references.values()) - REFERENCE_KEYS:
            raise failure("invalid_notification_registration")
        for value in (*match.values(), *references.keys()):
            safe_token(value)
        if set(arguments) & set(references):
            raise failure("notification_argument_overlap")
        if len(self.viewers) > 32 or len(set(self.recipients)) != len(self.recipients):
            raise failure("invalid_notification_recipients")
        # 単発結果を最新Resourceと取り違えない。登録時にTaskと実行回を固定する。
        if self.kind == "result" and not {"task_ref", "execution_ref"} <= set(match):
            raise failure("notification_result_identity_required")
        if self.detail.kind == "resource" and (arguments or references):
            raise failure("notification_resource_arguments_unsupported")

    @property
    def recipients(self) -> tuple[str, ...]:
        return (self.owner_user_id, *self.viewers)

    @property
    def signature(self) -> str:
        return digest([
            self.id, self.source_id, self.character_id, self.event_type,
            self.owner_user_id, self.auth_user_id, self.viewers, self.binding_id,
            self.kind, self.decision, self.detail.kind, self.detail.ref, self.detail.definition_digest,
            self.match_json, self.arguments_json, self.reference_arguments_json,
            self.origin_conversation, self.origin_message, self.budget_scope,
        ])

    @property
    def scope(self) -> str:
        match = json.loads(self.match_json)
        return self.budget_scope or match.get("rule_ref") or match.get("task_ref") or self.id

    @classmethod
    def parse(cls, value: Json) -> Registration:
        fields = {"id", "source_id", "character_id", "event_type", "detail", "owner_user_id", "auth_user_id", "viewers", "binding_id", "kind", "decision", "match", "arguments", "reference_arguments", "origin_conversation", "origin_message", "budget_scope"}
        if not isinstance(value, dict) or set(value) - fields:
            raise failure("invalid_notification_registration")
        if not isinstance(value.get("viewers", []), list):
            raise failure("invalid_notification_recipients")
        values = {k: v for k, v in value.items() if k not in {"detail", "match", "arguments", "reference_arguments", "viewers"}}
        return cls(**values, detail=Operation.parse(value["detail"]), viewers=tuple(value.get("viewers", ())),
                   match_json=encode(value.get("match", {})), arguments_json=encode(value.get("arguments", {})),
                   reference_arguments_json=encode(value.get("reference_arguments", {})))


def load_config(path: str | None) -> tuple[tuple[Registration, ...], Limits]:
    if path is None:
        return (), Limits()
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) - {"version", "registrations", "limits"} or type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError()
        registrations = tuple(Registration.parse(v) for v in value["registrations"])
        if len(registrations) > 256 or len({r.id for r in registrations}) != len(registrations):
            raise ValueError()
        return registrations, Limits(**value.get("limits", {}))
    except (OSError, ValueError, TypeError, KeyError, MCPFailure):
        raise ValueError("invalid DS_NOTIFICATION_CONFIG") from None


def metadata(value: Json, sanitizer: Sanitizer) -> Json:
    """参照と固定分類だけを保存し、許可名へ紛れ込んだ本文も受け付けない。"""
    kind = safe_token(value.get("type"), sanitizer)
    occurred = value.get("occurred_at")
    try:
        if not isinstance(occurred, str) or len(occurred) > 64:
            raise ValueError()
        stamp = datetime.fromisoformat(occurred)
        if stamp.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise failure("invalid_notification_time") from None
    result: Json = {"type": kind, "occurred_at": stamp.isoformat()}
    for key in REFERENCE_KEYS:
        if key in value:
            item = value[key]
            if key == "revision" and type(item) is int and 0 <= item <= 2**53 - 1:
                item = str(item)
            result[key] = safe_token(item, sanitizer)
    if isinstance(value.get("result_kind"), str) and value["result_kind"] in RESULT_KINDS:
        result["result_kind"] = value["result_kind"]
    return result


@dataclass(frozen=True)
class Reference:
    """後続Core consumerが独立保持する参照。HTTPから任意の参照を登録しない。"""
    registration_id: str
    registration_signature: str
    event_key: str
    metadata_json: str

    @property
    def metadata(self) -> Json:
        return json.loads(self.metadata_json)  # type: ignore[no-any-return]
