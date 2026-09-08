"""discoveryのstaged/active管理。現在loopへrefreshを混入させない。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import Connection, Discovery, MCPFailure, Snapshot, build_snapshot, now

Availability = Literal["unknown", "available", "degraded", "unavailable"]


@dataclass
class Entry:
    connection: Connection
    active: Snapshot | None = None
    staged: Snapshot | None = None
    availability: Availability = "unknown"
    desired_enabled: bool = True
    display_name: str = ""
    error_code: str | None = None
    last_checked_at: str | None = None
    healthy_operations: frozenset[str] = frozenset()
    generation: int = 0
    linked: bool = True


class Registry:
    def __init__(self) -> None:
        self._entries: dict[str, Entry] = {}

    def register(
        self,
        connection: Connection,
        *,
        desired_enabled: bool | None = None,
        display_name: str | None = None,
    ) -> None:
        old = self._entries.get(connection.id)
        if old is not None:
            if old.connection.identity != connection.identity:
                old.linked = False
                old.availability = "unavailable"
                raise MCPFailure("policy", "relink_required")
            if old.connection != connection:
                raise MCPFailure("policy", "registration_change_requires_relink")
            return
        self._entries[connection.id] = Entry(
            connection,
            desired_enabled=(
                connection.manifest["core_policy"]["enabled"]
                if desired_enabled is None
                else desired_enabled
            ),
            display_name=display_name or connection.id,
        )

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
        self.availability(connection.id, "unknown")
        # 再接続で旧loopを無効化。cached snapshotは診断にだけ残す。
        entry.staged = None

    def stage(self, connection_id: str, discovery: Discovery) -> Snapshot:
        entry = self.entry(connection_id)
        if not entry.linked:
            raise MCPFailure("policy", "relink_required")
        snapshot = build_snapshot(entry.connection, discovery)
        entry.staged = snapshot
        self.availability(connection_id, "available")
        return snapshot

    def activate(self) -> dict[str, tuple[Snapshot, int]]:
        result = {}
        for key, entry in self._entries.items():
            if (
                not entry.linked
                or not entry.desired_enabled
                or entry.availability not in {"available", "degraded"}
            ):
                continue
            if entry.staged is not None:
                entry.active = entry.staged.active()
                entry.staged = None
            if entry.active is not None:
                result[key] = (entry.active, entry.generation)
        return result

    def entries(self) -> tuple[Entry, ...]:
        return tuple(self._entries.values())

    def set_enabled(self, connection_id: str, enabled: bool) -> bool:
        entry = self.entry(connection_id)
        if entry.desired_enabled == enabled:
            return False
        entry.desired_enabled = enabled
        # OFF→ONでも古いloop、dispatch待ち、MRTRを復活させない。
        entry.generation += 1
        if enabled:
            self.availability(connection_id, "unknown")
        return True

    def availability(
        self,
        connection_id: str,
        state: Availability,
        *,
        error_code: str | None = None,
        healthy_operations: frozenset[str] = frozenset(),
    ) -> None:
        entry = self.entry(connection_id)
        if (
            state == "degraded"
            and entry.connection.manifest["connection"]["ownership"] != "self_owned"
        ):
            raise ValueError("partial health is only supported for self-owned addons")
        entry.availability = state
        codes = {
            "connection_failed",
            "authentication_failed",
            "protocol_error",
            "health_check_failed",
            "partial_failure",
        }
        if state == "degraded":
            entry.error_code = "partial_failure"
        elif state == "unavailable":
            entry.error_code = (
                error_code
                if error_code in codes - {"partial_failure"}
                else "connection_failed"
            )
        else:
            entry.error_code = None
        entry.healthy_operations = (
            healthy_operations if state == "degraded" else frozenset()
        )
        if state != "unknown":
            entry.last_checked_at = now()

    def operation_failure(self, connection_id: str, error: MCPFailure) -> None:
        # 単発timeout、5xx、引数不正等をconnection全体の障害に昇格しない。
        if error.category == "auth":
            self.availability(
                connection_id, "unavailable", error_code="authentication_failed"
            )
        elif error.code in {"connection_closed", "not_connected"}:
            self.availability(connection_id, "unavailable")
