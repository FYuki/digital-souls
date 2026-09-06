"""管理設定だけから外部MCPを接続し、Core全体でRegistry/Gateを共有する。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from app.external_mcp import Connection, ExecutionGate, ExternalMCPClient, Registry
from app.external_mcp.models import MCPFailure, encode
from app.inference import InferenceRouter
from app.privacy.contracts import PrivacyScanner

from .binding import BindingResolver, BindingTarget
from .projection import Sanitizer
from .routing import InferenceDecisionRouter
from .service import ToolService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolSettings:
    connections: tuple[Connection, ...] = ()
    bindings: tuple[BindingTarget, ...] = ()

    @classmethod
    def load(cls, path: str | None) -> ToolSettings:
        if not path:
            return cls()
        try:
            raw = Path(path).read_bytes()
            if len(raw) > 1_048_576:
                raise ValueError()
            value = json.loads(raw)
            if (
                not isinstance(value, dict)
                or set(value) - {"version", "connections", "bindings"}
                or type(value.get("version")) is not int
                or value.get("version") != 1
                or not isinstance(value.get("connections"), list)
                or not isinstance(value.get("bindings", []), list)
            ):
                raise ValueError()
            connections = tuple(
                Connection.from_manifest(c) for c in value["connections"]
            )
            ids = {c.id for c in connections}
            if len(ids) != len(connections) or len(connections) > 32:
                raise ValueError()
            targets = []
            for t in value.get("bindings", []):
                if set(t) - {
                    "id",
                    "connection_id",
                    "character_id",
                    "label",
                    "operations",
                    "arguments",
                }:
                    raise ValueError()
                if any(
                    not isinstance(t.get(k), str) or not t[k] or len(t[k]) > 128
                    for k in ("id", "connection_id", "character_id", "label")
                ):
                    raise ValueError()
                if (
                    t["connection_id"] not in ids
                    or not isinstance(t.get("operations"), list)
                    or not t["operations"]
                ):
                    raise ValueError()
                if any(
                    not isinstance(o, str) or not o for o in t["operations"]
                ) or not isinstance(t.get("arguments", {}), dict):
                    raise ValueError()
                targets.append(
                    BindingTarget(
                        t["id"],
                        t["connection_id"],
                        t["character_id"],
                        t["label"],
                        tuple(t["operations"]),
                        encode(t.get("arguments", {})),
                    )
                )
            if len({t.id for t in targets}) != len(targets) or len(targets) > 256:
                raise ValueError()
            return cls(connections, tuple(targets))
        except (OSError, ValueError, TypeError, KeyError, MCPFailure):
            # 設定ファイル本文・パス・認証値を起動例外へ含めない。
            raise ValueError("invalid DS_MCP_CONFIG") from None


class ToolRuntime:
    def __init__(
        self, settings: ToolSettings, router: InferenceRouter, scanner: PrivacyScanner
    ) -> None:
        self.settings = settings
        registry = Registry()
        bindings = BindingResolver(settings.bindings)
        private: list[str] = []
        references: list[str] = []
        for connection in settings.connections:
            registry.register(connection)
            config = connection.manifest["connection"]
            private.extend(
                [
                    config.get("endpoint", ""),
                    config.get("auth", {}).get("secret_ref", ""),
                ]
            )
            secret_ref = config.get("auth", {}).get("secret_ref")
            if secret_ref:
                references.append(secret_ref)
                private.append(os.environ.get(secret_ref, ""))
        self.gate = ExecutionGate(registry, bindings=bindings)
        protected = (
            Path(__file__).resolve().parents[3],
            Path(os.environ.get("DS_DATA_DIR", "data")),
        )
        self.service = ToolService(
            self.gate,
            InferenceDecisionRouter(router),
            Sanitizer(scanner, tuple(private), tuple(references)),
            bindings,
            protected_roots=protected,
        )
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        ready = []
        for connection in self.settings.connections:
            if not connection.manifest["core_policy"]["enabled"]:
                continue
            event = asyncio.Event()
            ready.append(event.wait())
            self._tasks.append(asyncio.create_task(self._connection(connection, event)))
        if ready:
            await asyncio.gather(*ready)

    async def _connection(self, connection: Connection, ready: asyncio.Event) -> None:
        while True:
            try:
                client = ExternalMCPClient(connection)
                async with client.connect(), self.gate.attach(connection.id, client):
                    ready.set()
                    while client.connected:
                        await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 共有serviceを止めず、接続失敗はmetadata-onlyで扱う。
                logger.warning("External MCP connection unavailable")
            finally:
                ready.set()
            await asyncio.sleep(5)

    async def close(self) -> None:
        self.service.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
