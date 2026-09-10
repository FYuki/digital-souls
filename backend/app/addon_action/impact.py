"""Coreの定義確認と実引数に基づく二段階判定。LLM・未信頼annotationは許可根拠にしない。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.external_mcp.models import Json, MCPFailure, digest, validate_arguments

from .models import OperationGroup


@dataclass(frozen=True)
class Impact:
    group: OperationGroup
    reason: str
    blocked: bool = False


def classify_static(manifest: Json, native: Json, effective: Json) -> Json:
    """登録時にCoreが管理するprofileを定義全体へ固定する。retry policyは変更しない。"""
    for profile in manifest["core_policy"].get("impact_profiles", []):
        if profile["tool_name"] != native["name"]:
            continue
        if profile["definition_digest"] != digest(native):
            return {"effect": "unknown", "source": "definition_changed"}
        return {
            "effect": profile["effect"],
            "source": "core_profile",
            "normal_arguments_schema": profile["normal_arguments_schema"],
        }
    if effective["effect"] == "read":
        return {"effect": "read", "source": effective["effect_source"]}
    # self-owned対応時も、検証済みeffective policyだけを受け取る。
    if effective["effect_source"] == "trusted_addon_metadata":
        return {"effect": effective["effect"], "source": "trusted_addon_metadata"}
    return {"effect": "unknown", "source": "unknown"}


_CONTROL = re.compile(
    r"^(action|operation|method|mode|command|permission|role|acl|visibility|access|scope)$",
    re.I,
)
_DANGEROUS_ACTION = re.compile(
    r"delete|remove|destroy|drop|truncate|force|reset|purchase|buy|pay|book|reserve|"
    r"subscribe|contract|grant|revoke|chmod|chown|admin|permission|credential|set_visibility|make_public|"
    r"削除|破棄|購入|予約|契約|課金|権限|認証",
    re.I,
)
_DESTRUCTIVE_FLAGS = {
    "force",
    "recursive",
    "permanent",
    "purge",
    "overwrite_history",
    "hard_delete",
}
_SECURITY_KEYS = {
    "acl",
    "permissions",
    "permission",
    "credential",
    "credentials",
    "role",
    "roles",
    "security",
}
_COUNT_KEYS = {"count", "quantity", "limit", "batch_size", "recipient_count"}
_TARGET_KEYS = {"targets", "recipients", "paths", "files", "ids", "operations"}
_PATH_KEY = re.compile(r"path|file|directory|root|database", re.I)


def evaluate_impact(
    static: Json,
    arguments: Json,
    *,
    binding_id: str | None = None,
    protected_roots: tuple[Path, ...] = (),
    force_confirmation: bool = False,
    bulk_threshold: int = 10,
) -> Impact:
    """binding解決済みの引数を評価する。profile条件外と判定不能はhigh impact。"""
    if bulk_threshold < 2:
        raise ValueError("invalid impact bulk threshold")
    high = OperationGroup.HIGH_IMPACT
    effect = static.get("effect", "unknown")
    changing = effect != "read"

    def inspect(value: object, key: str = "", depth: int = 0) -> Impact | None:
        if depth > 32:
            return Impact(high, "unclassified_arguments", True)
        if isinstance(value, dict):
            findings = [inspect(v, str(k), depth + 1) for k, v in value.items()]
        elif isinstance(value, list):
            findings = [inspect(v, key, depth + 1) for v in value]
        else:
            findings = []
        blocked = next((f for f in findings if f and f.blocked), None)
        if blocked:
            return blocked
        normalized = key.lower().replace("-", "_")
        if changing:
            if normalized in _DESTRUCTIVE_FLAGS and value not in (
                False,
                None,
                0,
                "false",
            ):
                return Impact(high, "destructive")
            if normalized in _SECURITY_KEYS:
                return Impact(high, "security")
            if (
                normalized in _COUNT_KEYS
                and isinstance(value, (int, float))
                and value >= bulk_threshold
            ):
                return Impact(high, "blast_radius")
            if (
                normalized in _TARGET_KEYS
                and isinstance(value, list)
                and len(value) >= bulk_threshold
            ):
                return Impact(high, "blast_radius")
            if isinstance(value, str):
                if _CONTROL.match(normalized) and _DANGEROUS_ACTION.search(value):
                    return Impact(high, "high_impact_action")
                if normalized in {
                    "new_visibility",
                    "new_access",
                    "new_scope",
                } and value.lower() in {"public", "everyone", "world", "all"}:
                    return Impact(high, "visibility_change")
                if protected_roots and "://" not in value.removeprefix("file://"):
                    if _PATH_KEY.search(key) or value.startswith(("/", "file://")):
                        path = Path(value.removeprefix("file://"))
                        if not path.is_absolute():
                            return Impact(high, "unresolved_write_path", True)
                        path = path.resolve()
                        if any(
                            path == root or root in path.parents or path in root.parents
                            for root in protected_roots
                        ):
                            return Impact(high, "core_write_denied", True)
        return next((f for f in findings if f), None)

    finding = inspect(arguments)
    if finding is not None:
        return finding
    if binding_id is not None and (not binding_id or len(binding_id) > 2048):
        return Impact(high, "unclassified_binding")
    if force_confirmation:
        return Impact(high, "core_requires_confirmation")
    if effect in {"unknown", "destructive", "security", "financial"}:
        return Impact(high, effect)
    if static.get("source") == "core_profile":
        try:
            validate_arguments(static["normal_arguments_schema"], arguments)
        except (KeyError, MCPFailure):
            return Impact(high, "outside_normal_arguments")
    elif effect != "read":
        # effectだけでは、大量操作や金銭的commitmentの上限を証明できない。
        return Impact(high, "unclassified_arguments")
    return Impact(OperationGroup.NORMAL, "classified_normal")
