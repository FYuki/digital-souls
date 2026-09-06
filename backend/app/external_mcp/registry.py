"""discoveryのstaged/active管理。現在loopへrefreshを混入させない。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import Connection, Discovery, MCPFailure, Snapshot, build_snapshot

Availability = Literal["available", "degraded", "unavailable"]


@dataclass
class Entry:
    connection: Connection
    active: Snapshot | None = None
    staged: Snapshot | None = None
    availability: Availability = "unavailable"
    generation: int = 0
    linked: bool = True


class Registry:
    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}

    def register(self, connection: Connection) -> None:
        old = self._entries.get(connection.id)
        if old is not None:
            if old.connection.identity != connection.identity:
                old.linked = False
                old.availability = "unavailable"
                raise MCPFailure("policy", "relink_required")
            if old.connection != connection:
                raise MCPFailure("policy", "registration_change_requires_relink")
            return
        self._entries[connection.id] = Entry(connection)

    def entry(self, connection_id: str) -> Entry:
        try:
            return self._entries[connection_id]
        except KeyError:
            raise MCPFailure("policy", "unknown_connection") from None

    def connected(self, connection: Connection) -> None:
        entry = self.entry(connection.id)
        if not entry.linked or entry.connection != connection:
            raise MCPFailure("policy", "relink_required")
        entry.generation += 1
        entry.availability = "unavailable"
        # 再接続で旧loopを無効化。cached snapshotは診断にだけ残す。
        entry.staged = None

    def stage(self, connection_id: str, discovery: Discovery) -> Snapshot:
        entry = self.entry(connection_id)
        if not entry.linked:
            raise MCPFailure("policy", "relink_required")
        snapshot = build_snapshot(entry.connection, discovery)
        entry.staged = snapshot
        entry.availability = "available"
        return snapshot

    def activate(self) -> dict[str, tuple[Snapshot, int]]:
        result = {}
        for key, entry in self._entries.items():
            if not entry.linked or entry.availability == "unavailable":
                continue
            if entry.staged is not None:
                entry.active = entry.staged.active()
                entry.staged = None
            if entry.active is not None:
                result[key] = (entry.active, entry.generation)
        return result

    def availability(self, connection_id: str, state: Availability) -> None:
        self.entry(connection_id).availability = state
