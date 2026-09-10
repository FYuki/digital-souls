"""管理設定だけから外部MCPを接続し、Core全体でRegistry/Gateを共有する。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.external_mcp import Connection, ExecutionGate, Registry
from app.external_mcp.models import MCPFailure, encode
from app.addon_admin.runtime import AddonRuntime
from app.addon_admin.connections import ConnectionStore
from app.addon_admin.management import ConnectionManagement
from app.inference import InferenceRouter
from app.privacy.contracts import PrivacyScanner
from app.privacy.semantic.classifier import SemanticPrivacyClassifier
from app.addon_action.egress import ActionEgress
from app.addon_action.policy import ActionPolicy
from app.addon_action.store import ActionStore

from .binding import BindingResolver, BindingTarget
from .projection import Sanitizer
from .routing import InferenceDecisionRouter
from .service import ToolService


@dataclass(frozen=True)
class ToolSettings:
    connections: tuple[Connection, ...] = ()
    bindings: tuple[BindingTarget, ...] = ()
    display_names: dict[str, str] = field(default_factory=dict)

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
                or set(value) - {"version", "connections", "bindings", "display_names"}
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
            display_names = value.get("display_names", {})
            if not isinstance(display_names, dict) or any(
                key not in ids
                or not isinstance(name, str)
                or not name.strip()
                or len(name) > 128
                or any(ord(char) < 32 for char in name)
                for key, name in display_names.items()
            ):
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
            return cls(connections, tuple(targets), display_names)
        except (OSError, ValueError, TypeError, KeyError, MCPFailure):
            # 設定ファイル本文・パス・認証値を起動例外へ含めない。
            raise ValueError("invalid DS_MCP_CONFIG") from None


class ToolRuntime:
    def __init__(
        self,
        settings: ToolSettings,
        router: InferenceRouter,
        scanner: PrivacyScanner,
        *,
        settings_path: Path | None = None,
        classifier: SemanticPrivacyClassifier | None = None,
    ) -> None:
        self.settings = settings
        registry = Registry()
        connection_store = (
            ConnectionStore(settings_path.parent / "mcp-admin" / "connections.sqlite3")
            if settings_path is not None
            else None
        )
        bindings = BindingResolver(settings.bindings)
        private: list[str] = []
        references: list[str] = []
        for connection in settings.connections:
            if (
                connection_store is not None
                and connection.manifest["connection"]["ownership"] == "external"
            ):
                auth = connection.manifest["connection"]["auth"]
                connection_store.import_connection(
                    connection,
                    settings.display_names.get(connection.id, connection.id),
                    os.environ.get(auth.get("secret_ref", ""))
                    if auth["type"] == "bearer"
                    else None,
                )
            else:
                registry.register(
                    connection, display_name=settings.display_names.get(connection.id)
                )
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
        if connection_store is not None:
            for record in connection_store.records():
                registry.register(
                    record.connection, display_name=record.spec.display_name
                )
        self.gate = ExecutionGate(registry, bindings=bindings)
        protected = (
            Path(__file__).resolve().parents[3],
            Path(os.environ.get("DS_DATA_DIR", "data")),
        )
        self.service = ToolService(
            self.gate,
            InferenceDecisionRouter(router),
            Sanitizer(
                scanner,
                tuple(private),
                tuple(references),
                dynamic_private_values=connection_store.private_values
                if connection_store
                else None,
            ),
            bindings,
            protected_roots=protected,
        )
        action_path = (
            (
                settings_path.parent
                if settings_path is not None
                else Path(os.environ.get("DS_DATA_DIR", "data"))
            )
            / "addon-actions"
            / "actions.sqlite3"
        )
        self.action_policy = ActionPolicy(
            ActionStore(action_path),
            self.service.sanitizer,
            egress=ActionEgress(classifier).allowed,
            protected_roots=protected,
            autonomous_wait_seconds=float(
                os.environ.get("DS_MCP_ACTION_WAIT_SECONDS", "60")
            ),
        )
        self.gate.confirmations = self.action_policy
        self.management: AddonRuntime = (
            ConnectionManagement(
                self.gate, connection_store, settings_path=settings_path
            )
            if connection_store is not None
            else AddonRuntime(self.gate, settings_path=settings_path)
        )
        self.management.on_disabled = self.service.connection_disabled

    async def start(self) -> None:
        self.action_policy.store.detach_waiters()
        await self.management.start()

    async def close(self) -> None:
        self.service.close()
        await self.management.close()
