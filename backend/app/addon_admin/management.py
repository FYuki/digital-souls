"""接続設定の変更と既存runtimeの同期。接続確認は実際の管理sessionを再利用する。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.external_mcp import ExecutionGate, ExternalMCPClient
from app.external_mcp.models import Connection, Json, MCPFailure
from app.external_mcp.registry import Entry

from .connection_models import ConnectionInput, CredentialInput
from .connections import ConnectionRecord, ConnectionStore
from .runtime import AddonRuntime, HealthPolicy
from pathlib import Path


class ConnectionManagement(AddonRuntime):
    def __init__(
        self,
        gate: ExecutionGate,
        connections: ConnectionStore,
        *,
        settings_path: Path | None = None,
        policy: HealthPolicy = HealthPolicy(),
        client_factory: Callable[[Connection], ExternalMCPClient] | None = None,
    ) -> None:
        super().__init__(
            gate,
            settings_path=settings_path,
            policy=policy,
            client_factory=client_factory
            or (lambda c: ExternalMCPClient(c, secrets=connections)),
        )
        self.connections = connections
        self._intent: dict[str, int] = {}
        self._owners: dict[str, asyncio.Task[None]] = {}
        self._changes: dict[str, asyncio.Lock] = {}
        self._checks: dict[str, list[tuple[int, asyncio.Future[bool]]]] = {}

    def _record(self, connection_id: str) -> ConnectionRecord:
        entry = self.registry.entry(connection_id)
        if entry.connection.manifest["connection"]["ownership"] != "external":
            raise MCPFailure("policy", "external_connection_required")
        return self.connections.get(connection_id)

    def _lock(self, connection_id: str) -> asyncio.Lock:
        return self._changes.setdefault(connection_id, asyncio.Lock())

    def _spawn(self, entry: Entry) -> None:
        cid = entry.connection.id
        self._wake.setdefault(cid, asyncio.Event())
        task = self._owners.get(cid)
        if task is None or task.done():
            task = asyncio.create_task(self._connection(entry))
            self._owners[cid] = task
            self._tasks.append(task)

    async def start(self) -> None:
        for entry in self.registry.entries():
            self._spawn(entry)

    def _complete(self, entry: Entry, success: bool) -> None:
        cid = entry.connection.id
        revision = self.connections.get(cid).revision
        for expected, future in self._checks.pop(cid, []):
            if not future.done():
                if expected == revision:
                    future.set_result(success)
                else:
                    future.set_exception(MCPFailure("policy", "connection_changed"))

    def _clean(
        self, value: object, maximum: int, private_values: tuple[str, ...]
    ) -> str:
        text = value if isinstance(value, str) else ""
        for private in sorted(private_values, key=len, reverse=True):
            text = text.replace(private, "[非公開]")
        return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)[:maximum]

    def _summary(self, entry: Entry) -> Json:
        snapshot = entry.staged or entry.active
        if snapshot is None:
            return {}
        data = snapshot.document
        private_values = self.connections.private_values()
        return {
            "counts": {
                key: len(data[key]) for key in ("tools", "resources", "prompts")
            },
            "tools": [
                {
                    "name": self._clean(tool["name"], 256, private_values),
                    "description": self._clean(
                        tool["native_definition"].get("description", ""),
                        4096,
                        private_values,
                    ),
                    "status": tool["status"],
                }
                for tool in data["tools"]
            ],
        }

    def _checked(self, entry: Entry) -> None:
        if entry.connection.manifest["connection"]["ownership"] != "external":
            return
        record = self.connections.get(entry.connection.id)
        self.connections.checked(
            entry.connection.id,
            record.revision,
            error_code=None,
            summary=self._summary(entry),
        )
        self._complete(entry, True)

    def _check_failed(self, entry: Entry, *, timed_out: bool = False) -> None:
        if entry.connection.manifest["connection"]["ownership"] != "external":
            return
        record = self.connections.get(entry.connection.id)
        self.connections.checked(
            entry.connection.id,
            record.revision,
            error_code="confirmation_timeout" if timed_out else entry.error_code,
        )
        self._requested.discard(entry.connection.id)
        self._complete(entry, False)

    def _status(self, entry: Entry) -> Json:
        status = self.projection(entry)
        if entry.connection.manifest["connection"]["ownership"] == "external":
            record = self.connections.get(entry.connection.id)
            status.update(
                settings_revision=record.revision,
                last_success_at=record.last_success_at,
                last_attempt_at=record.last_attempt_at,
                last_check_error=record.last_error_code,
            )
        return status

    def list(self) -> list[Json]:
        return [self._status(entry) for entry in self.registry.entries()]

    def detail(self, connection_id: str) -> Json:
        record = self._record(connection_id)
        return {
            **self._status(self.registry.entry(connection_id)),
            **record.spec.model_dump(),
            "credential_set": record.credential_set,
            "capabilities": record.summary,
        }

    async def check(self, connection_id: str) -> Json:
        async with self._lock(connection_id):
            record = self._record(connection_id)
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._checks.setdefault(connection_id, []).append((record.revision, future))
            self._requested.add(connection_id)
            entry = self.registry.entry(connection_id)
            self._spawn(entry)
            self._wake[connection_id].set()
        try:
            # 接続・healthの既存上限に、task起床の猶予だけを加える。
            await asyncio.wait_for(
                asyncio.shield(future),
                self.policy.connect_timeout + self.policy.timeout + 2,
            )
        except TimeoutError:
            raise MCPFailure("transport", "confirmation_timeout") from None
        finally:
            pending = self._checks.get(connection_id, [])
            self._checks[connection_id] = [
                (rev, f) for rev, f in pending if f is not future
            ]
            if not future.done():
                future.cancel()
        return self.detail(connection_id)

    async def enable(self, connection_id: str, enabled: bool) -> Json:
        entry = self.registry.entry(connection_id)
        if entry.connection.manifest["connection"]["ownership"] != "external":
            return self.set_enabled(connection_id, enabled)
        self._record(connection_id)
        intent = self._intent.get(connection_id, 0) + 1
        self._intent[connection_id] = intent
        if not enabled:
            # OFFで確認待ちのON要求も無効化し、後着の応答から復活させない。
            async with self._lock(connection_id):
                self._cancel_checks(connection_id)
                self.set_enabled(connection_id, False)
                return self._status(self.registry.entry(connection_id))
        expected = self._record(connection_id).revision
        result = await self.check(connection_id)
        async with self._lock(connection_id):
            if (
                self._record(connection_id).revision != expected
                or self._intent.get(connection_id) != intent
            ):
                raise MCPFailure("policy", "connection_changed")
            if result["availability"] != "available":
                raise MCPFailure("unavailable", "connection_unconfirmed")
            self.set_enabled(connection_id, True)
            entry = self.registry.entry(connection_id)
            self.registry.availability(connection_id, "available")
            self._confirmed_generation[connection_id] = entry.generation
            return self._status(entry)

    async def create(self, spec: ConnectionInput) -> Json:
        record = self.connections.create(spec)
        self.registry.register(
            record.connection, desired_enabled=False, display_name=spec.display_name
        )
        self.store.initial(record.connection.id, False)
        return await self.check(record.connection.id)

    def _cancel_checks(self, connection_id: str) -> None:
        for _, future in self._checks.pop(connection_id, []):
            if not future.done():
                future.set_exception(MCPFailure("policy", "connection_changed"))
        self._requested.discard(connection_id)
        self._confirmed_generation.pop(connection_id, None)

    async def _stop_owner(self, connection_id: str) -> None:
        self._cancel_checks(connection_id)
        owner = self._owners.pop(connection_id, None)
        if owner is not None:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            if owner in self._tasks:
                self._tasks.remove(owner)

    async def update(self, connection_id: str, spec: ConnectionInput) -> Json:
        async with self._lock(connection_id):
            old = self._record(connection_id)
            proposed = spec.connection(connection_id, old.secret_ref, old.connection)
            if proposed == old.connection:
                self.connections.update(connection_id, spec)
                self.registry.entry(connection_id).display_name = spec.display_name
                return self.detail(connection_id)
            with self.gate.edit_connection(connection_id):
                # 設定検証と実行中判定を終えてから旧sessionを終了する。
                await self._stop_owner(connection_id)
                try:
                    record = self.connections.update(connection_id, spec)
                except BaseException:
                    self._spawn(self.registry.entry(connection_id))
                    raise
                entry = self.registry.replace(
                    record.connection, display_name=spec.display_name
                )
                self.gate.invalidate_connection(connection_id)
                self.on_disabled(connection_id)
                self._spawn(entry)
        return await self.check(connection_id)

    async def credential(self, connection_id: str, payload: CredentialInput) -> Json:
        async with self._lock(connection_id):
            old = self._record(connection_id)
            if old.connection.manifest["connection"]["auth"]["type"] != "bearer":
                raise MCPFailure("validation", "credential_not_applicable")
            with self.gate.edit_connection(connection_id):
                await self._stop_owner(connection_id)
                try:
                    record = self.connections.credential(connection_id, payload)
                except BaseException:
                    self._spawn(self.registry.entry(connection_id))
                    raise
                entry = self.registry.replace(
                    record.connection, display_name=record.spec.display_name
                )
                self.gate.invalidate_connection(connection_id)
                self.on_disabled(connection_id)
                self._spawn(entry)
        return await self.check(connection_id)

    async def delete(self, connection_id: str) -> None:
        async with self._lock(connection_id):
            self._record(connection_id)
            with self.gate.edit_connection(connection_id):
                await self._stop_owner(connection_id)
                try:
                    self.connections.delete(connection_id)
                except BaseException:
                    self._spawn(self.registry.entry(connection_id))
                    raise
                self.gate.invalidate_connection(connection_id)
                self.on_disabled(connection_id)
                self.registry.remove(connection_id)
                self._wake.pop(connection_id, None)
