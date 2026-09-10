"""#300 操作群の判定と承認正本。副作用の安全判定をTool名へ依存させない。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from app.addon_action.impact import evaluate_impact
from app.addon_action.models import (
    ApprovalChoice,
    ApprovalKey,
    ExecutionScene,
    OperationGroup,
    Permission,
)
from app.addon_action.store import ActionStore
from app.external_mcp import Connection, Registry
from app.external_mcp.models import MCPFailure, build_snapshot, digest
from tests.external_mcp_test_support import discovery, manifest


def key(**changes):
    return replace(
        ApprovalKey(
            "external-test",
            "identity",
            OperationGroup.HIGH_IMPACT,
            ExecutionScene.CONVERSATION,
        ),
        **changes,
    )


def test_defaults_persistence_and_all_permission_dimensions(tmp_path):
    path = tmp_path / "action.sqlite3"
    store = ActionStore(path)
    assert store.state(key()).permission == Permission.UNAPPROVED
    assert store.state(key(group=OperationGroup.NORMAL)).permission == Permission.ALWAYS
    store.choose(key(), ApprovalChoice.ALWAYS)
    restored = ActionStore(path)
    assert restored.consume(key())
    assert restored.consume(key())
    for other in [
        key(connection_id="other"),
        key(connection_identity="relinked"),
        key(scene=ExecutionScene.AUTONOMOUS),
    ]:
        assert not restored.consume(other)


def test_once_is_atomic_across_store_instances(tmp_path):
    path = tmp_path / "action.sqlite3"
    store = ActionStore(path)
    store.choose(key(), ApprovalChoice.ONCE)
    stores = [ActionStore(path) for _ in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda s: s.consume(key()), stores))
    assert sum(results) == 1
    assert not store.state(key()).allowed


def test_reject_has_different_lifetime_per_scene(tmp_path):
    store = ActionStore(tmp_path / "action.sqlite3")
    store.choose(key(), ApprovalChoice.REJECT)
    assert store.state(key()).permission == Permission.UNAPPROVED
    autonomous = key(scene=ExecutionScene.AUTONOMOUS)
    store.choose(autonomous, ApprovalChoice.REJECT)
    assert ActionStore(store.path).state(autonomous).permission == Permission.DENIED
    store.choose(autonomous, ApprovalChoice.ALWAYS)
    assert store.consume(autonomous)
    assert not store.consume(key())


def classified(*, trusted=False, effect=None, normal_schema=None, data=None):
    data = data or discovery()
    config = manifest(trusted=trusted)
    if effect:
        config["core_policy"]["impact_profiles"] = [
            {
                "tool_name": "native-tool",
                "definition_digest": digest(data.tools[0]),
                "effect": effect,
                "normal_arguments_schema": normal_schema or {"type": "object"},
            }
        ]
    connection = Connection.from_manifest(config)
    return build_snapshot(connection, data).document["tools"][0], connection


def test_unknown_annotations_do_not_authorize_even_if_name_says_read():
    tool, _ = classified()
    assert (
        evaluate_impact(tool["impact_classification"], {"value": 1}).group
        == OperationGroup.HIGH_IMPACT
    )
    trusted, _ = classified(trusted=True)
    assert (
        evaluate_impact(trusted["impact_classification"], {"value": 1}).group
        == OperationGroup.NORMAL
    )


def test_verified_normal_update_does_not_relax_retry_and_checks_actual_arguments():
    tool, _ = classified(
        effect="write",
        normal_schema={
            "type": "object",
            "properties": {"value": {"type": "integer", "maximum": 5}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    static = tool["impact_classification"]
    assert tool["effective_policy"] == {
        "effect": "unknown",
        "effect_source": "unknown",
        "retry": "none",
        "concurrency": "serial",
    }
    assert (
        evaluate_impact(static, {"value": 2}, binding_id="selected-target").group
        == OperationGroup.NORMAL
    )
    assert (
        evaluate_impact(static, {"value": 6}, binding_id="selected-target").group
        == OperationGroup.HIGH_IMPACT
    )
    assert (
        evaluate_impact(static, {"value": 1, "extra": True}).group
        == OperationGroup.HIGH_IMPACT
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"force": True},
        {"action": "delete"},
        {"permissions": []},
        {"action": "purchase"},
        {"action": "reserve"},
        {"recipients": list(range(10))},
        {"quantity": 10},
        {"new_visibility": "public"},
        {"nested": {"operation": "grant"}},
    ],
)
def test_actual_impact_overrides_normal_profile(arguments):
    tool, _ = classified(effect="external_send")
    assert (
        evaluate_impact(tool["impact_classification"], arguments).group
        == OperationGroup.HIGH_IMPACT
    )


def test_regular_external_send_and_public_content_are_not_automatically_high():
    tool, _ = classified(effect="external_send")
    assert (
        evaluate_impact(
            tool["impact_classification"],
            {"message": "公開の話題を投稿する", "public": True, "visibility": "public"},
        ).group
        == OperationGroup.NORMAL
    )


def test_core_write_is_blocked_even_when_other_high_impact_is_present(tmp_path):
    tool, _ = classified(effect="write")
    impact = evaluate_impact(
        tool["impact_classification"],
        {"force": True, "path": str(tmp_path / "config")},
        protected_roots=(tmp_path,),
    )
    assert impact.blocked
    assert impact.reason == "core_write_denied"
    nested = evaluate_impact(
        tool["impact_classification"],
        {
            "permissions": {"path": str(tmp_path / "config")},
        },
        protected_roots=(tmp_path,),
    )
    assert nested.blocked


def test_core_profile_never_retains_conflicting_read_optimization():
    tool, _ = classified(trusted=True, effect="write")
    assert tool["effective_policy"]["concurrency"] == "serial"
    assert tool["effective_policy"]["retry"] == "none"


def test_refresh_changes_static_classification_only_in_next_snapshot():
    tool, connection = classified(effect="write")
    registry = Registry()
    registry.register(connection)
    registry.stage(connection.id, discovery())
    original = registry.activate()[connection.id][0]
    updated = discovery()
    updated.tools[0]["description"] = "意味が変わったTool"
    registry.stage(connection.id, updated)
    assert (
        original.document["tools"][0]["impact_classification"]["source"]
        == "core_profile"
    )
    new = registry.activate()[connection.id][0]
    assert new.revision != original.revision
    assert new.document["tools"][0]["impact_classification"]["effect"] == "unknown"


def test_profile_duplicate_and_invalid_normal_schema_are_rejected():
    _, connection = classified(effect="write")
    value = connection.manifest
    value["core_policy"]["impact_profiles"] *= 2
    with pytest.raises(MCPFailure, match="duplicate_impact_profile"):
        Connection.from_manifest(value)
    value["core_policy"]["impact_profiles"] = value["core_policy"]["impact_profiles"][
        :1
    ]
    value["core_policy"]["impact_profiles"][0]["normal_arguments_schema"] = {
        "type": "invalid"
    }
    with pytest.raises(MCPFailure, match="invalid_input_schema"):
        Connection.from_manifest(value)


def test_core_restriction_still_requires_high_impact_approval():
    tool, _ = classified(trusted=True)
    assert (
        evaluate_impact(
            tool["impact_classification"], {"value": 1}, force_confirmation=True
        ).group
        == OperationGroup.HIGH_IMPACT
    )
