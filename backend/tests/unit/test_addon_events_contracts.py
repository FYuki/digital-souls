"""有限契約、設定、process crash時のSQLite transaction境界。"""
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.addon_events.contracts import Limits, Operation, Source, Baseline, Event, Page, load_sources, parse_page
from app.addon_events.runtime import EventRuntime
from app.addon_events.store import EventStore
from app.external_mcp import ExecutionGate, Registry, MCPFailure
from app.tool_use.projection import Sanitizer
from tests.tool_use_test_support import Scanner

SOURCE = Source("fixture", "external-test", "miori",
                Operation("tool", "history", "sha256:" + "0" * 64),
                Operation("tool", "snapshot", "sha256:" + "0" * 64))


@pytest.mark.parametrize("kwargs", [
    {"retention_seconds": 72 * 3600 + 1}, {"poll_seconds": 0}, {"poll_seconds": float("nan")},
    {"poll_seconds": "60"}, {"max_events": True}, {"page_size": 129}, {"max_bytes": 2**30},
    {"retry_initial": 301}, {"max_event_bytes": 1},
])
def test_limits_reject_unbounded_or_invalid(kwargs):
    with pytest.raises(MCPFailure, match="invalid_event_limits"):
        Limits(**kwargs)


def test_profile_config_is_explicit_and_bounded(tmp_path):
    path = tmp_path / "sources.json"
    source = {"id": "fixture", "connection_id": "external-test", "character_id": "miori",
              "history": {"kind": "tool", "ref": "history", "definition_digest": "sha256:" + "0" * 64},
              "snapshot": {"kind": "resource", "ref": "events://fixture/snapshot", "definition_digest": "sha256:" + "1" * 64}}
    path.write_text(json.dumps({"version": 1, "sources": [source]}))
    assert load_sources(str(path))[0].limits.poll_seconds == 60
    assert load_sources(str(path))[0].limits.retention_seconds == 72 * 3600
    path.write_text(json.dumps({"version": 1, "sources": [source, {**source, "id": "second"}]}))
    with pytest.raises(ValueError, match="invalid DS_MCP_EVENT_CONFIG"):
        load_sources(str(path))
    assert load_sources(None) == ()


def test_single_owner_and_source_identity_pins(tmp_path):
    path = tmp_path / "events.sqlite3"
    gate = ExecutionGate(Registry())
    sanitizer = Sanitizer(Scanner())
    first = EventRuntime(gate, (SOURCE,), path, sanitizer)
    try:
        with pytest.raises(MCPFailure, match="event_store_owned"):
            EventRuntime(gate, (SOURCE,), path, sanitizer)
        first.store.pin_identity(SOURCE.id, "identity-1")
        with pytest.raises(MCPFailure, match="event_connection_identity_changed"):
            first.store.pin_identity(SOURCE.id, "identity-2")
        with pytest.raises(MCPFailure, match="event_source_registration_changed"):
            first.store.configure(replace(SOURCE, character_id="other"))
    finally:
        import asyncio
        asyncio.run(first.close())


def test_os_crash_rolls_back_buffer_and_cursor(tmp_path):
    path = tmp_path / "events.sqlite3"
    store = EventStore(path)
    store.configure(SOURCE)
    store.register(SOURCE.id, "consumer", SOURCE.context)
    store.baseline(SOURCE.id, Baseline("era1", "era1~10", 10), gap=False)
    store.close()
    # Core processがtransaction途中で強制終了する。MCP serverの代用ではなく永続化境界の単体試験。
    script = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("BEGIN IMMEDIATE")
db.execute("INSERT INTO event_buffer VALUES ('fixture',11,'era1~11','key','{}',NULL,1,130)")
db.execute("UPDATE event_sources SET position=11,cursor='era1~11' WHERE id='fixture'")
os._exit(73)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path)], check=False)
    assert result.returncode == 73
    store = EventStore(path)
    try:
        assert store.state(SOURCE.id)["position"] == 10
        assert store.diagnostics(SOURCE.id)["buffer_count"] == 0
        assert store.consumer(SOURCE.id, "consumer")["position"] == 10
        store.commit_page(SOURCE, Page("era1", "era1~11", 11, (Event(11, "era1~11", "key"),), False),
                          expected_cursor="era1~10")
    finally:
        store.close()
    store = EventStore(path)
    assert store.state(SOURCE.id)["position"] == 11
    assert len(store.buffered(SOURCE, 10)) == 1
    store.close()


@pytest.mark.parametrize("extra", [
    {"position": True}, {"more": "yes"}, {"more": True}, {"events": [None]},
    {"events": [], "position": 11}, {"epoch": "https://secret.invalid"},
])
def test_invalid_outer_envelope_never_advances(extra):
    value = {"epoch": "era1", "next_cursor": "era1~10", "position": 10, "events": [], "more": False, **extra}
    with pytest.raises(MCPFailure):
        parse_page(value, after=10, epoch="era1", sanitizer=Sanitizer(Scanner()), limits=Limits())


def test_cross_page_id_conflict_is_atomic(tmp_path):
    store = EventStore(tmp_path / "events.sqlite3")
    try:
        store.configure(SOURCE)
        store.baseline(SOURCE.id, Baseline("era1", "era1~10", 10), gap=False)
        store.commit_page(SOURCE, Page("era1", "era1~11", 11, (Event(11, "era1~11", "same-key"),), False),
                          expected_cursor="era1~10")
        with pytest.raises(MCPFailure, match="conflicting_event_id"):
            store.commit_page(SOURCE, Page("era1", "era1~12", 12, (Event(12, "era1~12", "same-key"),), False),
                              expected_cursor="era1~11")
        assert store.state(SOURCE.id)["position"] == 11
        assert store.diagnostics(SOURCE.id)["buffer_count"] == 1
    finally:
        store.close()


def test_public_schema_snapshot_and_history_examples():
    from app.external_mcp.models import validate_contract
    snapshot = {"epoch": "era1", "cursor": "era1~10", "position": 10, "state": {"count": 10}}
    history = {"epoch": "era1", "next_cursor": "era1~11", "position": 11, "events": [{
        "position": 11, "cursor": "era1~11", "id": "event-11", "type": "task.completed",
        "occurred_at": "2026-09-16T00:00:00+00:00", "metadata": {"task_ref": "task-11"},
    }], "more": False}
    validate_contract("event-history", snapshot)
    validate_contract("event-history", history)
    page = parse_page(history, after=10, epoch="era1", sanitizer=Sanitizer(Scanner()), limits=Limits())
    assert page.position == 11 and page.events[0].metadata["task_ref"] == "task-11"


def test_fixture_has_no_core_imports():
    import ast
    fixture = Path(__file__).parents[1] / "fixtures/addon_events/server.py"
    modules = []
    for node in ast.walk(ast.parse(fixture.read_text())):
        if isinstance(node, ast.Import):
            modules.extend(name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    assert all(name != "app" and not name.startswith("app.") for name in modules)
