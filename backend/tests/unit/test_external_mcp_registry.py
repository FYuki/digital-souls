"""#153の単体検証。"""

import pytest
from app.external_mcp import Connection, MCPFailure, Registry
from app.external_mcp.models import build_snapshot
from tests.external_mcp_test_support import discovery, manifest


@pytest.mark.parametrize(
    "trusted,parallel,retry",
    [(False, "serial", "none"), (True, "parallel", "read_once")],
)
def test_trust_is_independent_of_permission(trusted, parallel, retry):
    c = Connection.from_manifest(manifest(trusted=trusted))
    snap = build_snapshot(c, discovery()).document
    tool = snap["tools"][0]
    assert tool["effective_policy"]["concurrency"] == parallel
    assert tool["effective_policy"]["retry"] == retry
    assert tool["native_definition"] == discovery().tools[0]
    assert tool["trust"]["addon_metadata"] is False
    snap["tools"].clear()
    assert build_snapshot(c, discovery()).document["tools"]


@pytest.mark.parametrize(
    "change",
    [
        lambda m: m["connection"]["auth"].update(token="raw"),
        lambda m: m["core_policy"]["restrictions"].append(
            {"target": {"tool_name": "native-tool"}, "action": "force_read_only"}
        ),
        lambda m: m["connection"].update(
            endpoint="http://user:password@localhost/mcp", transport="streamable_http"
        ),
        lambda m: m["connection"].update(expected_identity={"tls_issuer": "unchecked"}),
    ],
)
def test_invalid_registration(change):
    value = manifest()
    change(value)
    with pytest.raises(MCPFailure):
        Connection.from_manifest(value)


def test_default_trust_and_immutable_config():
    value = manifest()
    value["connection"].pop("trust")
    c = Connection.from_manifest(value)
    assert not c.manifest["connection"]["trust"]["annotations"]
    c.manifest["core_policy"]["enabled"] = False
    assert c.manifest["core_policy"]["enabled"]


def test_identity_change_requires_relink():
    registry = Registry()
    value = manifest()
    c = Connection.from_manifest(value)
    registry.register(c)
    registry.stage(c.id, discovery())
    value["connection"]["stdio"]["args"] = ["different-server"]
    with pytest.raises(MCPFailure, match="relink_required"):
        registry.register(Connection.from_manifest(value))
    assert not registry.activate()


def test_snapshot_schema_revision_and_validation():
    c = Connection.from_manifest(manifest())
    before = build_snapshot(c, discovery()).document
    d = discovery()
    d.tools[0]["inputSchema"]["properties"]["value"]["type"] = "string"
    after = build_snapshot(c, d).document
    assert before["snapshot_revision"] != after["snapshot_revision"]
    assert before["tools"][0]["schema_digest"] != after["tools"][0]["schema_digest"]
    with pytest.raises(MCPFailure):
        build_snapshot(c, discovery("same", "same"))
    d.tools[0]["annotations"]["readOnlyHint"] = "true"
    with pytest.raises(MCPFailure):
        build_snapshot(c, d)


@pytest.mark.parametrize("action", ["force_serial", "disable_retry"])
def test_safe_override_never_promotes_unknown(action):
    for trust in [True, False]:
        c = Connection.from_manifest(
            manifest(
                trusted=trust,
                restrictions=[
                    {"target": {"tool_name": "native-tool"}, "action": action}
                ],
            )
        )
        p = build_snapshot(c, discovery()).document["tools"][0]["effective_policy"]
        if action == "force_serial" or not trust:
            assert p["concurrency"] == "serial"
        if action == "disable_retry" or not trust:
            assert p["retry"] == "none"


def test_remote_schema_ref_cannot_trigger_network_fetch():
    from app.external_mcp.models import validate_arguments

    with pytest.raises(MCPFailure, match="invalid_arguments"):
        validate_arguments({"$ref": "http://127.0.0.1:18000/private"}, {})


def test_invalid_output_schema_is_rejected_before_activation():
    c = Connection.from_manifest(manifest())
    data = discovery()
    data.tools[0]["outputSchema"] = {"type": "invalid-type"}
    with pytest.raises(MCPFailure):
        build_snapshot(c, data)


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "localhost", "[::1]", "example.com", "192.168.1.10"]
)
def test_preconfigured_bearer_http_is_limited_to_local_loopback(host):
    value = manifest(
        transport="streamable_http",
        endpoint=f"http://{host}:9100/mcp",
        auth={"type": "bearer", "secret_ref": "MCP_TOKEN"},
    )
    if host in {"127.0.0.1", "localhost", "[::1]"}:
        assert (
            Connection.from_manifest(value).manifest["connection"]["auth"]["type"]
            == "bearer"
        )
    else:
        with pytest.raises(MCPFailure, match="tls_required"):
            Connection.from_manifest(value)
