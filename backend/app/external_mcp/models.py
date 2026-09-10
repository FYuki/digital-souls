"""外部MCPの検証済み設定と不変なnative source model。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource

Json = dict[str, Any]
CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "addon"


class MCPFailure(Exception):
    """外部本文を含めない固定エラーコード。"""

    def __init__(self, category: str, code: str, *, retryable: bool = False) -> None:
        self.category = category
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def encode(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(encode(value).encode()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@lru_cache
def contract_validator(name: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS / f"{name}.schema.json").read_text())
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_contract(name: str, value: Any) -> None:
    if not contract_validator(name).is_valid(value):
        raise MCPFailure("validation", f"invalid_{name}")


def _no_remote(uri: str) -> Resource[Any]:
    raise NoSuchResource(uri)


def input_validator(schema: Json) -> Draft202012Validator:
    try:
        Draft202012Validator.check_schema(schema)
        # 外部schemaの検証でネットワークやファイルを取得しない。
        return Draft202012Validator(
            schema,
            registry=Registry(retrieve=_no_remote),  # type: ignore[call-arg]
        )
    except Exception:
        raise MCPFailure("validation", "invalid_input_schema") from None


def validate_arguments(schema: Json, arguments: Json) -> None:
    try:
        input_validator(schema).validate(arguments)
    except Exception:
        raise MCPFailure("validation", "invalid_arguments") from None


@dataclass(frozen=True)
class Connection:
    """登録は管理側だけが行う。JSON文字列で呼出元からの変更を防ぐ。"""

    _json: str = field(repr=False)

    @classmethod
    def from_manifest(cls, manifest: Json) -> Connection:
        try:
            value = json.loads(encode(manifest))
        except (TypeError, ValueError):
            raise MCPFailure("validation", "invalid_manifest") from None
        value.get("connection", {}).setdefault(
            "trust", {"annotations": False, "addon_metadata": False}
        )
        validate_contract("manifest", value)
        c = value["connection"]
        if c["ownership"] != "external" or c["trust"]["addon_metadata"]:
            raise MCPFailure("policy", "unsupported_self_owned")
        if c.get("expected_identity"):
            # 検証できないTLS issuer/auth principal制約を無視しない。
            raise MCPFailure("policy", "unsupported_identity_constraint")
        if c["transport"] == "streamable_http":
            url = urlsplit(c["endpoint"])
            if (
                not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise MCPFailure("validation", "unsafe_endpoint")
            if url.scheme != "https" and url.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise MCPFailure("validation", "tls_required")
        for rule in value["core_policy"]["restrictions"]:
            if "stable_operation_id" in rule["target"]:
                raise MCPFailure("policy", "unsupported_stable_operation_id")
        profiles = value["core_policy"].get("impact_profiles", [])
        if len({p["tool_name"] for p in profiles}) != len(profiles):
            raise MCPFailure("validation", "duplicate_impact_profile")
        for profile in profiles:
            input_validator(profile["normal_arguments_schema"])
        return cls(encode(value))

    @property
    def manifest(self) -> Json:
        return json.loads(self._json)  # type: ignore[no-any-return]

    @property
    def id(self) -> str:
        return str(self.manifest["connection"]["id"])

    @property
    def identity(self) -> str:
        # serverInfoやannotationをidentityとして使わない。
        c = self.manifest["connection"]
        return digest({k: v for k, v in c.items() if k not in {"trust", "grant"}})

    def restrictions(self, name: str) -> frozenset[str]:
        policy = self.manifest["core_policy"]
        allowlist = policy.get("operation_allowlist")
        if allowlist is not None and name not in allowlist:
            return frozenset({"deny"})
        return frozenset(
            r["action"]
            for r in policy["restrictions"]
            if r["target"].get("tool_name") == name
        )


@dataclass(frozen=True)
class Discovery:
    protocol_version: str
    tools: tuple[Json, ...] = ()
    resources: tuple[Json, ...] = ()
    prompts: tuple[Json, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    _json: str = field(repr=False)

    @property
    def document(self) -> Json:
        return json.loads(self._json)  # type: ignore[no-any-return]

    @property
    def revision(self) -> str:
        return str(self.document["snapshot_revision"])

    def active(self) -> Snapshot:
        value = self.document
        value["activation_state"] = "active"
        return Snapshot(encode(value))


def build_snapshot(connection: Connection, discovery: Discovery) -> Snapshot:
    from app.addon_action.impact import classify_static

    trusted = connection.manifest["connection"]["trust"]["annotations"]
    tools: list[Json] = []
    for native in discovery.tools:
        schema = native.get("inputSchema")
        if not isinstance(schema, dict):
            raise MCPFailure("validation", "missing_input_schema")
        input_validator(schema)
        if "outputSchema" in native:
            output_schema = native["outputSchema"]
            if not isinstance(output_schema, dict):
                raise MCPFailure("validation", "invalid_output_schema")
            input_validator(output_schema)
        annotations = native.get("annotations") or {}
        if not isinstance(annotations, dict):
            raise MCPFailure("validation", "invalid_annotations")
        for key in (
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        ):
            if key in annotations and type(annotations[key]) is not bool:
                raise MCPFailure("validation", "invalid_annotations")
        read = trusted and annotations.get("readOnlyHint") is True
        rules = connection.restrictions(native.get("name", ""))
        policy = {
            "effect": "read" if read else "unknown",
            "effect_source": "trusted_annotation" if read else "unknown",
            "concurrency": "parallel"
            if read and "force_serial" not in rules
            else "serial",
            "retry": "read_once" if read and "disable_retry" not in rules else "none",
        }
        execution = native.get("execution") or {}
        if not isinstance(execution, dict):
            raise MCPFailure("validation", "invalid_execution_metadata")
        unsupported = execution.get("taskSupport") == "required"
        impact = classify_static(connection.manifest, native, policy)
        if impact["effect"] != "read" and read:
            # Coreが副作用・定義変更を認識した場合はread annotationの最適化を抑止する。
            policy = {
                "effect": "unknown",
                "effect_source": "unknown",
                "concurrency": "serial",
                "retry": "none",
            }
        tool = {
            "name": native.get("name"),
            "input_schema": schema,
            "schema_digest": digest(
                {"inputSchema": schema, "outputSchema": native.get("outputSchema")}
            ),
            "native_definition": native,
            "native_annotations": annotations,
            "native_meta": native.get("_meta") or {},
            "trust": {"annotations": trusted, "addon_metadata": False},
            "effective_policy": policy,
            "impact_classification": impact,
            "status": "unsupported" if unsupported else "active",
        }
        if unsupported:
            tool["unsupported_reason"] = "tasks_unsupported"
        tools.append(tool)
    resources = list(discovery.resources)
    prompts = list(discovery.prompts)
    for definitions, key in ((tools, "name"), (resources, "uri"), (prompts, "name")):
        names = [d.get(key) for d in definitions]
        if any(not isinstance(n, str) or not n for n in names) or len(
            set(names)
        ) != len(names):
            raise MCPFailure("validation", "duplicate_or_missing_capability")
        definitions.sort(key=lambda d: d[key])
    payload = {
        "connection_instance_id": connection.id,
        "protocol_version": discovery.protocol_version,
        "tools": tools,
        "resources": resources,
        "prompts": prompts,
    }
    value = {
        **payload,
        "snapshot_revision": digest(payload),
        "discovered_at": now(),
        "activation_state": "staged",
    }
    validate_contract("capability-snapshot", value)
    return Snapshot(encode(value))
