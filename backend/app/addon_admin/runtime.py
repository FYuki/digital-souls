"""LLMから独立した接続監視。OFFは既存sessionやprocessを停止しない。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.external_mcp import Connection, ExecutionGate, ExternalMCPClient
from app.external_mcp.models import MCPFailure, now
from app.external_mcp.registry import Entry

from .store import SettingsStore


@dataclass(frozen=True)
class HealthPolicy:
    interval: float = 30
    timeout: float = 5
    failures: int = 3
    connect_timeout: float = 20
    retry_initial: float = 5
    retry_max: float = 300
    disconnect_poll: float = 1

    def __post_init__(self) -> None:
        if (
            min(
                self.interval,
                self.timeout,
                self.failures,
                self.connect_timeout,
                self.retry_initial,
                self.retry_max,
                self.disconnect_poll,
            )
            <= 0
        ):
            raise ValueError("health limits must be positive")


class AddonRuntime:
    def __init__(
        self,
        gate: ExecutionGate,
        *,
        settings_path: Path | None = None,
        policy: HealthPolicy = HealthPolicy(),
        client_factory: Callable[[Connection], ExternalMCPClient] = ExternalMCPClient,
    ) -> None:
        self.gate = gate
        self.registry = gate.registry
        self.store = SettingsStore(settings_path)
        self.policy = policy
        self.client_factory = client_factory
        self.on_disabled: Callable[[str], None] = lambda _: None
        self._tasks: list[asyncio.Task[None]] = []
        self._wake: dict[str, asyncio.Event] = {}
        for entry in self.registry.entries():
            entry.desired_enabled = self.store.initial(
                entry.connection.id, entry.desired_enabled
            )
            self._wake[entry.connection.id] = asyncio.Event()

    @staticmethod
    def projection(entry: Entry) -> dict[str, object]:
        return {
            "connection_instance_id": entry.connection.id,
            "display_name": entry.display_name,
            "source_type": entry.connection.manifest["connection"]["ownership"],
            "desired_enabled": entry.desired_enabled,
            "availability": entry.availability,
            "effective_state": entry.availability
            if entry.desired_enabled
            else "disabled",
            "error_code": entry.error_code,
            "last_checked_at": entry.last_checked_at,
        }

    def list(self) -> list[dict[str, object]]:
        return [self.projection(entry) for entry in self.registry.entries()]

    def set_enabled(self, connection_id: str, enabled: bool) -> dict[str, object]:
        entry = self.registry.entry(connection_id)
        # 保存に失敗した場合、実行中の希望値は変更しない。同期区間内であと勝ちを確定。
        self.store.save(connection_id, enabled)
        if self.registry.set_enabled(connection_id, enabled):
            if not enabled:
                self.gate.invalidate_connection(connection_id)
                self.on_disabled(connection_id)
            self._wake[connection_id].set()
        return self.projection(entry)

    async def start(self) -> None:
        if self._tasks:
            return
        for entry in self.registry.entries():
            self._tasks.append(asyncio.create_task(self._connection(entry)))

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _wait(self, connection_id: str, delay: float) -> None:
        event = self._wake[connection_id]
        try:
            await asyncio.wait_for(event.wait(), delay)
        except TimeoutError:
            pass
        event.clear()

    def _failed(self, entry: Entry, error: Exception, *, health: bool = False) -> None:
        code = "health_check_failed" if health else "connection_failed"
        if isinstance(error, MCPFailure):
            if error.category == "auth":
                code = "authentication_failed"
            elif error.category in {"protocol", "validation", "policy"}:
                code = "protocol_error"
        self.registry.availability(entry.connection.id, "unavailable", error_code=code)

    async def _connection(self, entry: Entry) -> None:
        connection_id = entry.connection.id
        delay = self.policy.retry_initial
        while True:
            if not entry.desired_enabled:
                await self._wait(connection_id, self.policy.interval)
                continue
            try:
                client = self.client_factory(entry.connection)
                # timeoutは接続・初回discoveryだけに適用し、session全体を期限切れにしない。
                async with asyncio.timeout(self.policy.connect_timeout) as deadline:
                    async with (
                        client.connect(),
                        self.gate.attach(connection_id, client),
                    ):
                        deadline.reschedule(None)
                        delay = self.policy.retry_initial
                        await self._monitor(entry, client)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._failed(entry, error)
            await self._wait(connection_id, delay)
            delay = min(delay * 2, self.policy.retry_max)

    async def _monitor(self, entry: Entry, client: ExternalMCPClient) -> None:
        failures = 0
        checked_generation = entry.generation
        next_check = asyncio.get_running_loop().time() + self.policy.interval
        while True:
            if not client.connected:
                raise client.connection_failure or MCPFailure(
                    "unavailable", "connection_closed"
                )
            current = asyncio.get_running_loop().time()
            if entry.desired_enabled and (
                entry.generation != checked_generation or current >= next_check
            ):
                generation = entry.generation
                if generation != checked_generation:
                    failures = 0
                checked_generation = generation
                try:
                    async with asyncio.timeout(self.policy.timeout):
                        await client.health()
                    if generation != entry.generation:
                        continue
                    if entry.availability in {"unknown", "unavailable"}:
                        async with asyncio.timeout(self.policy.connect_timeout):
                            await self.gate.refresh(entry.connection.id)
                    if generation == entry.generation:
                        # 部分障害はpingだけでは解消しない。自作のhealth集約側が復旧させる。
                        if entry.availability != "degraded":
                            self.registry.availability(entry.connection.id, "available")
                        failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    if generation == entry.generation:
                        failures += 1
                        definite = isinstance(error, MCPFailure) and error.category in {
                            "auth",
                            "protocol",
                            "validation",
                        }
                        if (
                            definite
                            or not client.connected
                            or failures >= self.policy.failures
                        ):
                            self._failed(entry, error, health=True)
                if generation == entry.generation:
                    entry.last_checked_at = now()
                next_check = asyncio.get_running_loop().time() + self.policy.interval
            await self._wait(
                entry.connection.id,
                min(self.policy.disconnect_poll, self.policy.interval),
            )
