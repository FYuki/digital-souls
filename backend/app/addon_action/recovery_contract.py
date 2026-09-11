"""管理側が定義を固定して採用する外部操作の回復契約。MCP metadataは根拠にしない。"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from app.external_mcp.models import (
    Connection,
    Json,
    MCPFailure,
    digest,
    encode,
    validate_arguments,
)

from .journal import ActionOutcome


def recovery_profile(
    connection: Connection, operation: str, tools: list[Json]
) -> Json | None:
    profile = next(
        (
            p
            for p in connection.manifest["core_policy"].get("recovery_profiles", [])
            if p["tool_name"] == operation
        ),
        None,
    )
    if profile is None:
        return None
    original = next((t for t in tools if t["name"] == operation), None)
    if original is None:
        raise MCPFailure("recovery", "recovery_definition_changed")
    schema = original["input_schema"]
    field = profile["request_key_argument"]
    if (
        field in schema.get("required", [])
        or schema.get("properties", {}).get(field, {}).get("type") != "string"
    ):
        # LLMへ公開するnative schemaを変更せず、Core専用の任意引数だけを補う。
        raise MCPFailure("recovery", "invalid_request_key_contract")
    # 操作と回復Toolの両方を固定する。定義変更時は無保証で送信する方へfallbackしない。
    refs = [profile, profile["status"]] + [
        profile[k] for k in ("replay", "cancel") if k in profile
    ]
    for ref in refs:
        tool = next((t for t in tools if t["name"] == ref["tool_name"]), None)
        if (
            tool is None
            or tool["status"] != "active"
            or digest(tool["native_definition"]) != ref["definition_digest"]
        ):
            raise MCPFailure("recovery", "recovery_definition_changed")
        if "deny" in connection.restrictions(ref["tool_name"]):
            raise MCPFailure("policy", "operation_denied")
    return dict(profile)


def request_key(scope: str, character: str, scene: str, fingerprint: str) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            encode(["digital-souls-action-v1", character, scene, scope, fingerprint]),
        )
    )


def bind_request_key(profile: Json, arguments: Json, key: str, schema: Json) -> Json:
    field = profile["request_key_argument"]
    if field in arguments:
        raise MCPFailure("validation", "reserved_action_request_key")
    bound = {**arguments, field: key}
    validate_arguments(schema, bound)
    return bound


def decode_action(payload: Json) -> tuple[ActionOutcome, str | None, Json]:
    """request-key-v1採用時だけ呼ぶ。通常MCP結果の同名fieldを信用しない。"""
    structured = payload.get("structuredContent")
    action = structured.get("action") if isinstance(structured, dict) else None
    if not isinstance(action, dict) or payload.get("isError"):
        return ActionOutcome.RESULT_UNKNOWN, None, {}
    try:
        outcome = ActionOutcome(action.get("status", ""))
    except (ValueError, TypeError):
        return ActionOutcome.RESULT_UNKNOWN, None, {}
    if outcome in {ActionOutcome.DISPATCHING, ActionOutcome.INPUT_REQUIRED}:
        return ActionOutcome.RESULT_UNKNOWN, None, {}
    task_id = action.get("task_id")
    if task_id is not None and (
        not isinstance(task_id, str) or not 1 <= len(task_id) <= 256
    ):
        return ActionOutcome.RESULT_UNKNOWN, None, {}
    # 外部の正文・状態は非信頼データとして通常のSanitizerへ渡す。
    result = {key: action[key] for key in ("result", "latest_state") if key in action}
    return outcome, task_id, result
