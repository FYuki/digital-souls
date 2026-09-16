"""Event履歴の公開結果契約と、Coreが登録する取得設定。"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from app.external_mcp import ExecutionContext
from app.external_mcp.models import Json, MCPFailure, digest, encode
from app.tool_use.projection import Sanitizer

TOKEN = re.compile(r"^[A-Za-z0-9_.:~-]{1,256}$")
METADATA_KEYS = frozenset({
    "resource_ref", "task_ref", "rule_ref", "execution_ref", "revision",
    "result_kind", "status", "count",
})


def failure(code: str) -> MCPFailure:
    return MCPFailure("event", code)


def token(value: Any, sanitizer: Sanitizer | None = None) -> str:
    if not isinstance(value, str) or not TOKEN.fullmatch(value):
        raise failure("invalid_event_token")
    if sanitizer is not None and sanitizer.text(value) != value:
        raise failure("unsafe_event_token")
    return value


def position(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2**53 - 1:
        raise failure("invalid_event_position")
    return int(value)


@dataclass(frozen=True)
class Operation:
    kind: Literal["tool", "resource"]
    ref: str = field(repr=False)
    definition_digest: str

    @classmethod
    def parse(cls, value: Json) -> Operation:
        if (
            not isinstance(value, dict)
            or set(value) != {"kind", "ref", "definition_digest"}
            or value.get("kind") not in {"tool", "resource"}
            or not isinstance(value.get("ref"), str)
            or not 1 <= len(value["ref"]) <= 2048
            or not isinstance(value.get("definition_digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["definition_digest"])
        ):
            raise failure("invalid_event_operation")
        return cls(value["kind"], value["ref"], value["definition_digest"])


@dataclass(frozen=True)
class Limits:
    poll_seconds: float = 60.0
    retention_seconds: float = 72 * 3600
    max_events: int = 10_000
    max_bytes: int = 16 * 1024 * 1024
    page_size: int = 128
    max_event_bytes: int = 16 * 1024
    retry_initial: float = 5.0
    retry_max: float = 300.0

    def __post_init__(self) -> None:
        for value in (self.poll_seconds, self.retention_seconds, self.retry_initial, self.retry_max):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise failure("invalid_event_limits")
        if self.retention_seconds > 72 * 3600 or self.retry_initial > self.retry_max:
            raise failure("invalid_event_limits")
        for value in (self.max_events, self.max_bytes, self.page_size, self.max_event_bytes):
            if type(value) is not int or value < 1:
                raise failure("invalid_event_limits")
        if self.page_size > 128 or self.max_events > 100_000 or self.max_bytes > 128 * 1024 * 1024:
            raise failure("invalid_event_limits")
        if not 256 <= self.max_event_bytes <= min(self.max_bytes, 64 * 1024):
            raise failure("invalid_event_limits")


@dataclass(frozen=True)
class Source:
    id: str
    connection_id: str
    character_id: str
    history: Operation
    snapshot: Operation
    user_id: str | None = None
    binding_id: str | None = None
    arguments_json: str = field(default="{}", repr=False)
    wake_resource: str | None = field(default=None, repr=False)
    limits: Limits = Limits()

    def __post_init__(self) -> None:
        for value in (self.id, self.connection_id, self.character_id):
            token(value)
        for optional_value in (self.user_id, self.binding_id):
            if optional_value is not None:
                token(optional_value)
        if self.wake_resource is not None and (not isinstance(self.wake_resource, str) or not 1 <= len(self.wake_resource) <= 2048):
            raise failure("invalid_event_wake_resource")
        arguments = json.loads(self.arguments_json)
        if not isinstance(arguments, dict) or set(arguments) & {"cursor", "limit"}:
            raise failure("invalid_event_arguments")

    @property
    def context(self) -> ExecutionContext:
        # 会話IDではなく、再試行でも変わらない取得予算の単位。
        return ExecutionContext(self.character_id, f"event:{self.id}", self.user_id, self.binding_id)

    @property
    def signature(self) -> str:
        return digest([
            self.connection_id, self.character_id, self.user_id, self.binding_id,
            self.history.kind, self.history.ref, self.history.definition_digest,
            self.snapshot.kind, self.snapshot.ref, self.snapshot.definition_digest,
            self.arguments_json,
        ])

    @classmethod
    def parse(cls, value: Json) -> Source:
        allowed = {"id", "connection_id", "character_id", "user_id", "binding_id",
                   "history", "snapshot", "arguments", "wake_resource", "limits"}
        if not isinstance(value, dict) or set(value) - allowed:
            raise failure("invalid_event_source")
        return cls(
            id=value["id"], connection_id=value["connection_id"],
            character_id=value["character_id"],
            history=Operation.parse(value["history"]), snapshot=Operation.parse(value["snapshot"]),
            user_id=value.get("user_id"), binding_id=value.get("binding_id"),
            arguments_json=encode(value.get("arguments", {})),
            wake_resource=value.get("wake_resource"), limits=Limits(**value.get("limits", {})),
        )


def load_sources(path: str | None) -> tuple[Source, ...]:
    if path is None:
        return ()
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"version", "sources"}:
            raise ValueError()
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError()
        if not isinstance(value["sources"], list) or len(value["sources"]) > 32:
            raise ValueError()
        sources = tuple(Source.parse(item) for item in value["sources"])
        if len({s.id for s in sources}) != len(sources) or len({s.connection_id for s in sources}) != len(sources):
            raise ValueError()
        return sources
    except (OSError, ValueError, TypeError, KeyError, MCPFailure):
        raise ValueError("invalid DS_MCP_EVENT_CONFIG") from None


def project_metadata(value: Any, sanitizer: Sanitizer) -> Json:
    if not isinstance(value, dict):
        raise failure("invalid_event_metadata")
    result: Json = {}
    for key in METADATA_KEYS:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, str) and len(item) <= 256:
            # 参照を伏字にすると別の参照になるため、安全でない項目は除外する。
            if sanitizer.text(item, maximum=256) == item:
                result[key] = item
        elif type(item) in {int, bool} and (type(item) is bool or abs(item) <= 2**53 - 1):
            result[key] = item
    return result


@dataclass(frozen=True)
class Event:
    position: int
    cursor: str = field(repr=False)
    key: str
    metadata_json: str = "{}"
    reason: str | None = None

    @property
    def metadata(self) -> Json:
        return json.loads(self.metadata_json)  # type: ignore[no-any-return]


@dataclass(frozen=True)
class Page:
    epoch: str = field(repr=False)
    cursor: str = field(repr=False)
    position: int
    events: tuple[Event, ...]
    more: bool
    gap: bool = False


@dataclass(frozen=True)
class Baseline:
    epoch: str = field(repr=False)
    cursor: str = field(repr=False)
    position: int
    metadata_json: str = "{}"


def parse_baseline(value: Json, sanitizer: Sanitizer) -> Baseline:
    try:
        return Baseline(
            token(value["epoch"], sanitizer), token(value["cursor"], sanitizer),
            position(value["position"]), encode(project_metadata(value["state"], sanitizer)),
        )
    except (KeyError, TypeError, ValueError):
        raise failure("invalid_event_snapshot") from None


def parse_page(value: Json, *, after: int, epoch: str, sanitizer: Sanitizer, limits: Limits) -> Page:
    try:
        page_epoch = token(value["epoch"], sanitizer)
        cursor = token(value["next_cursor"], sanitizer)
        end = position(value["position"])
        gap = value.get("gap", False)
        more = value["more"]
        rows = value["events"]
        if type(gap) is not bool or type(more) is not bool or not isinstance(rows, list) or len(rows) > limits.page_size * 2:
            raise failure("invalid_event_page")
        if gap or page_epoch != epoch:
            return Page(page_epoch, cursor, end, (), more, True)
        if end < after or end - after > limits.page_size:
            raise failure("invalid_event_page")
        events: dict[int, Event] = {}
        keys: set[str] = set()
        for row in rows:
            # 外側の位置・cursorが壊れている場合は安全に飛ばせない。
            pos = position(row["position"])
            row_cursor = token(row["cursor"], sanitizer)
            if pos <= after:
                continue
            if pos > end:
                raise failure("invalid_event_page")
            try:
                event_id = token(row["id"], sanitizer)
                kind = token(row["type"], sanitizer)
                metadata = project_metadata(row.get("metadata", {}), sanitizer)
                occurred = row.get("occurred_at")
                if not isinstance(occurred, str):
                    raise failure("invalid_event_time")
                stamp = datetime.fromisoformat(occurred)
                if stamp.tzinfo is None:
                    raise failure("invalid_event_time")
                event_json = encode({"type": kind, "occurred_at": stamp.isoformat(), **metadata})
                if len(event_json.encode()) > limits.max_event_bytes:
                    raise failure("event_too_large")
                item = Event(pos, row_cursor, digest([epoch, event_id]), event_json)
            except (MCPFailure, KeyError, ValueError, TypeError):
                item = Event(pos, row_cursor, digest([epoch, pos]), reason="invalid_event")
            previous = events.get(pos)
            if previous is not None:
                if previous != item:
                    raise failure("conflicting_event_position")
                continue
            if item.key in keys:
                raise failure("conflicting_event_id")
            keys.add(item.key)
            events[pos] = item
        if sorted(events) != list(range(after + 1, end + 1)):
            raise failure("event_sequence_gap")
        if events and events[end].cursor != cursor:
            raise failure("event_cursor_mismatch")
        if more and end == after:
            raise failure("event_cursor_stalled")
        return Page(page_epoch, cursor, end, tuple(events[p] for p in sorted(events)), more)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise failure("invalid_event_page") from None
