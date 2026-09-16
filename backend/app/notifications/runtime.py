"""共有Eventの独立consumer、通知Policy、UI向け利用契約。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal

from app.addon_events.runtime import EventRuntime
from app.external_mcp import ExecutionContext, ExecutionGate
from app.external_mcp.models import Json, MCPFailure
from app.tool_use.projection import Sanitizer
from .contracts import Limits, Registration, failure, metadata
from .reader import NotificationReader
from .store import NotificationStore


class NotificationRuntime:
    def __init__(self, gate: ExecutionGate, events: EventRuntime | None, registrations: tuple[Registration, ...],
                 path: Path, sanitizer: Sanitizer, *, character_exists: Callable[[str], bool],
                 limits: Limits = Limits(), store: NotificationStore | None = None) -> None:
        if len({r.id for r in registrations}) != len(registrations) or len(registrations) > 256:
            raise failure("invalid_notification_registrations")
        self.store = store or NotificationStore(path, limits)
        self.reader = NotificationReader(gate, events, self.store, sanitizer, character_exists=character_exists)
        self.registrations = self.reader.registrations = {r.id: r for r in registrations}
        self.events, self.sanitizer = events, sanitizer
        self._lock = asyncio.Lock()
        self._workers: list[asyncio.Task[None]] = []
        self._subscribed: set[str] = set()
        self._status: dict[str, str] = {}
        self._closed = False
        try:
            for registration in registrations:
                self.store.configure(registration)
        except BaseException:
            self.store.close()
            raise

    def _context(self, registration: Registration) -> ExecutionContext:
        source = self.reader.source(registration)
        return replace(source.context, character_id=registration.character_id, user_id=registration.auth_user_id)

    async def _subscribe(self, registration: Registration) -> None:
        if registration.id not in self._subscribed:
            assert self.events is not None
            await self.events.subscribe(registration.source_id, f"notification:{registration.id}", self._context(registration))
            self._subscribed.add(registration.id)

    async def consume_once(self, registration_id: str) -> None:
        async with self._lock:
            registration = self.registrations[registration_id]
            await self.reader.authorize(registration)
            await self._subscribe(registration)
            assert self.events is not None
            delivery = await self.events.read(registration.source_id, f"notification:{registration.id}", self._context(registration))
            output: list[Json] = []
            gaps = list(delivery.gaps)
            for event in delivery.events:
                try:
                    value = metadata(event["metadata"], self.sanitizer)
                    # 他Policy向けのイベントの参照は、この登録の詳細対象へ流用しない。
                    if value["type"] != registration.event_type or any(value.get(k) != v for k, v in json.loads(registration.match_json).items()):
                        continue
                    self.reader.arguments(registration, value)
                    await self.reader.authorize(registration, value=value)
                    permitted = []
                    for viewer in registration.recipients:
                        try:
                            await self.reader.authorize(registration, viewer=viewer, value=value)
                            permitted.append(viewer)
                        except MCPFailure:
                            continue
                    output.append({**event, "metadata": value, "permitted_users": permitted})
                except MCPFailure:
                    gaps.append({"reason": "invalid_event", "position": event["position"]})
            await self.reader.authorize(registration)
            self.store.apply_batch(registration, delivery.stream, delivery.position, tuple(output), gaps=tuple(gaps))
            await self.events.acknowledge(registration.source_id, f"notification:{registration.id}", delivery.receipt,
                                          self._context(registration), accept_gap=True)
            self._status[registration.id] = "ready"

    async def _worker(self, registration: Registration) -> None:
        errors = 0
        while not self._closed:
            try:
                async with asyncio.timeout(60):
                    await self.consume_once(registration.id)
                errors = 0
            except (MCPFailure, OSError, sqlite3.Error, TimeoutError):
                # raw例外、native本文、参照をlogへ流さず、有限backoffで再確認する。
                self._status[registration.id] = "unavailable"
                errors = min(errors + 1, 6)
            await asyncio.sleep(min(300, self.store.limits.poll_seconds * 2 ** errors))

    async def _maintenance(self) -> None:
        while not self._closed:
            try:
                self.store.prune()
            except (OSError, sqlite3.Error):
                pass
            await asyncio.sleep(60)

    def start(self) -> None:
        if self._workers or self._closed:
            return
        self._workers = [asyncio.create_task(self._worker(r)) for r in self.registrations.values()]
        self._workers.append(asyncio.create_task(self._maintenance()))

    async def close(self) -> None:
        self._closed = True
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        # 登録を解除せず再起動後に同じconsumer位置から続ける。
        self.store.close()

    async def allowed(self, viewer: str) -> set[str]:
        allowed: set[str] = set()
        for registration in self.registrations.values():
            try:
                await self.reader.authorize(registration, viewer=viewer)
                allowed.add(registration.id)
            except MCPFailure:
                continue
        return allowed

    def _item(self, row: Json) -> Json:
        value = metadata(json.loads(row["metadata"]), self.sanitizer)
        registration = self.registrations[row["registration"]]
        return {key: row[key] for key in ("id", "source_id", "character_id", "event_type", "created", "expires", "state", "version")} | {
            "metadata": value, "kind": registration.kind,
        }

    def source_status(self, registration: Registration) -> str:
        if self.events is None or registration.source_id not in self.events.sources:
            return "unavailable"
        status = self.events.store.state(registration.source_id)["status"]
        if status in {"backoff", "stopped"}:
            return "unavailable"
        return self._status.get(registration.id, "starting")

    async def listing(self, viewer: str, *, source_id: str | None = None, character_id: str | None = None,
                      unread: bool = False, hidden: bool = False, offset: int = 0, limit: int = 50) -> Json:
        allowed = await self.allowed(viewer)
        result = self.store.listing(viewer, allowed, source_id=source_id, character_id=character_id,
                                    unread=unread, hidden=hidden, offset=offset, limit=limit)
        items = []
        for row in result["items"]:
            try:
                items.append(self._item(row))
            except MCPFailure:
                # 秘密値の追加登録後に安全でなくなった参照を再表示しない。
                continue
        result["items"] = items
        result["sources"] = [
            {"source_id": r.source_id, "event_type": r.event_type, "character_id": r.character_id,
             "enabled": self.store.decision(r, viewer) == "notify",
             "configurable": r.decision != "state-only", "available": r.id in allowed,
             "status": self.source_status(r),
             "history_incomplete": self.store.registration(r.id)["gap_count"] > 0}
            for r in self.registrations.values() if viewer in r.recipients and self.reader.character_exists(r.character_id)
            and all(self.sanitizer.text(token) == token for token in (r.source_id, r.event_type, r.character_id))
        ]
        return result

    async def _authorized_row(self, notification_id: str, viewer: str) -> Json:
        row = self.store.get(notification_id, viewer)
        registration = self.registrations.get(row["registration"])
        if registration is None:
            raise failure("notification_not_found")
        await self.reader.authorize(registration, viewer=viewer, value=json.loads(row["metadata"]))
        return row

    async def set_state(self, notification_id: str, viewer: str, state: Literal["read", "unread", "hidden"], version: int) -> Json:
        await self._authorized_row(notification_id, viewer)
        return self._item(self.store.set_state(notification_id, viewer, state, version))

    async def detail(self, notification_id: str, viewer: str) -> Json:
        try:
            async with asyncio.timeout(self.store.limits.read_seconds):
                row = await self._authorized_row(notification_id, viewer)
                reference = self.reader.reference(row["registration"], row["event_key"], json.loads(row["metadata"]))
                # 取得成功では既読を変更しない。画面への提示後の明示state APIを境界にする。
                return await self.reader.read(reference, viewer)
        except TimeoutError:
            return {"state": "unavailable"}

    async def set_preference(self, viewer: str, source_id: str, event_type: str, enabled: bool) -> None:
        async with self._lock:
            registrations = tuple(r for r in self.registrations.values()
                                  if r.source_id == source_id and r.event_type == event_type and viewer in r.recipients)
            if not registrations or any(r.decision == "state-only" for r in registrations):
                raise failure("notification_preference_unavailable")
            fences: list[tuple[str, str, int]] = []
            for registration in registrations:
                # OFFは接続停止中も可能。ONは現在権限・source基準位置が必要。
                if enabled:
                    await self.reader.authorize(registration, viewer=viewer)
                    await self._subscribe(registration)
            if enabled:
                assert self.events is not None
                stream, position = await self.events.received_checkpoint(source_id)
                fences = [(registration.id, stream, position) for registration in registrations]
            self.store.set_preference(viewer, source_id, event_type, enabled, fences=tuple(fences))
