"""通知の寿命、原子性、有限予算、Policyの単体試験。"""
from dataclasses import replace
import json

import pytest

from app.addon_events.contracts import Operation
from app.external_mcp.models import MCPFailure, digest, encode
from app.notifications.contracts import Limits, Registration, metadata
from app.notifications.store import NotificationStore
from app.tool_use.projection import Sanitizer
from tests.addon_events_test_support import Clock
from tests.tool_use_test_support import Scanner


def registration(**kwargs):
    return Registration(**(dict(id="r", source_id="s", character_id="miori", event_type="updated",
                               detail=Operation("tool", "detail", digest({}))) | kwargs))


def event(position=1, **kwargs):
    return {"position": position, "key": digest(["event", position]),
            "metadata": {"type": "updated", "occurred_at": "2000-01-01T00:00:00+00:00", "resource_ref": "resource-a"} | kwargs}


def test_policy_default_ignore_state_only_and_user_overrides(tmp_path):
    store = NotificationStore(tmp_path / "n.db")
    try:
        ignored = registration()
        observed = registration(id="observed", decision="state-only")
        notify = registration(id="notify", decision="notify", viewers=("second",))
        for reg in (ignored, observed, notify):
            store.configure(reg)
        store.set_preference("local", "s", "updated", True)
        store.set_preference("second", "s", "updated", False)
        assert store.decision(ignored, "unconfigured") == "ignore"
        assert store.decision(observed, "local") == "state-only"
        store.apply_batch(observed, "stream", 1, (event(),))
        assert store.registration(observed.id)["observed"] != "{}"
        assert store.listing("local", {observed.id})["total"] == 0
        store.apply_batch(notify, "stream", 1, (event(),))
        assert store.listing("local", {notify.id})["total"] == 1
        assert store.listing("second", {notify.id})["total"] == 0
    finally:
        store.close()


def test_retention_is_first_save_time_and_deleted_replays_stay_deleted(tmp_path):
    clock = Clock()
    store = NotificationStore(tmp_path / "n.db", clock=clock)
    reg = registration(decision="notify")
    store.configure(reg)
    try:
        store.apply_batch(reg, "stream", 1, (event(),))
        row = store.listing("local", {reg.id})["items"][0]
        assert row["expires"] == clock() + 30 * 86400
        clock.advance(29 * 86400)
        store.set_state(row["id"], "local", "hidden", 1)
        store.apply_batch(reg, "stream", 1, (event(),))
        assert store.get(row["id"], "local")["expires"] == row["expires"]
        clock.advance(86400)
        store.apply_batch(reg, "stream", 1, (event(),))
        assert store.listing("local", {reg.id}, hidden=True)["total"] == 0
        assert store.registration(reg.id)["position"] == 1
    finally:
        store.close()


def test_batch_rolls_back_notifications_and_checkpoint_together(tmp_path, monkeypatch):
    store = NotificationStore(tmp_path / "n.db")
    reg = registration(decision="notify")
    store.configure(reg)
    original = store._prune
    def broken(_):
        raise OSError("synthetic write failure")
    try:
        monkeypatch.setattr(store, "_prune", broken)
        with pytest.raises(OSError):
            store.apply_batch(reg, "stream", 2, (event(1), event(2)))
        assert store.registration(reg.id)["position"] == 0
        assert store.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0
        monkeypatch.setattr(store, "_prune", original)
        store.apply_batch(reg, "stream", 2, (event(1), event(2)))
        assert store.listing("local", {reg.id})["total"] == 2
    finally:
        store.close()


def test_cap_is_per_user_and_does_not_extend_with_reads(tmp_path):
    clock = Clock()
    store = NotificationStore(tmp_path / "n.db", Limits(max_per_user=2), clock=clock)
    first, other = registration(decision="notify"), registration(id="other", owner_user_id="other", decision="notify")
    try:
        for reg in (first, other):
            store.configure(reg)
            store.apply_batch(reg, "stream", 1, (event(),))
        clock.advance()
        store.apply_batch(first, "stream", 3, (event(2), event(3)))
        result = store.listing("local", {first.id})
        assert result["total"] == 2 and result["unread_count"] == 2
        assert result["retention"]["history_incomplete"]
        assert store.listing("other", {other.id})["total"] == 1
        clock.advance(30 * 86400)
        assert not store.listing("local", {first.id})["retention"]["history_incomplete"]
    finally:
        store.close()


def test_budget_cannot_reset_by_reopening_store_or_new_registration(tmp_path):
    clock = Clock()
    limits = Limits(scope_reads_per_minute=2)
    reg = registration(budget_scope="same-task")
    store = NotificationStore(tmp_path / "n.db", limits, clock=clock)
    store.charge_read(reg)
    store.charge_read(reg)
    store.close()
    store = NotificationStore(tmp_path / "n.db", limits, clock=clock)
    try:
        with pytest.raises(MCPFailure, match="notification_budget_exceeded"):
            store.charge_read(replace(reg, id="new-registration"))
        clock.advance(61)
        store.charge_read(reg)
    finally:
        store.close()


def test_registration_changes_and_unsafe_metadata_fail_closed(tmp_path):
    store = NotificationStore(tmp_path / "n.db")
    reg = registration()
    try:
        store.configure(reg)
        with pytest.raises(MCPFailure):
            store.configure(replace(reg, character_id="other"))
        store.pin(reg, "identity", "binding-a")
        with pytest.raises(MCPFailure):
            store.pin(reg, "identity", "binding-b")
        sanitizer = Sanitizer(Scanner(), ("synthetic-secret",))
        with pytest.raises(MCPFailure):
            metadata(event()["metadata"] | {"resource_ref": "synthetic-secret"}, sanitizer)
        projected = metadata(event()["metadata"] | {"body": "private body", "result_kind": {}}, sanitizer)
        assert "body" not in projected and "result_kind" not in projected
    finally:
        store.close()


def test_invalid_recipient_container_is_rejected():
    with pytest.raises(MCPFailure):
        Registration.parse({"id": "r", "source_id": "s", "character_id": "miori", "event_type": "updated",
                            "viewers": "joe", "detail": {"kind": "tool", "ref": "detail", "definition_digest": digest({})}})


def test_old_delivery_does_not_remove_fence_for_newer_stream(tmp_path):
    store = NotificationStore(tmp_path / "n.db")
    reg = registration(decision="ignore")
    store.configure(reg)
    try:
        store.set_preference("local", reg.source_id, reg.event_type, True,
                             fences=((reg.id, "new-stream", 10),))
        store.apply_batch(reg, "old-stream", 3, (event(3),))
        store.apply_batch(reg, "new-stream", 10, (event(10),))
        assert store.listing("local", {reg.id})["total"] == 0
        store.apply_batch(reg, "new-stream", 11, (event(11),))
        assert store.listing("local", {reg.id})["total"] == 1
    finally:
        store.close()


@pytest.mark.parametrize("expired_state", ["unread", "read", "hidden"])
def test_read_paths_filter_expiry_without_write_transactions(tmp_path, expired_state):
    clock = Clock()
    store = NotificationStore(tmp_path / "n.db", Limits(retention_seconds=60), clock=clock)
    reg = registration(decision="notify")
    store.configure(reg)
    try:
        store.apply_batch(reg, "stream", 1, (event(1),))
        old = store.listing("local", {reg.id})["items"][0]
        if expired_state != "unread":
            store.set_state(old["id"], "local", expired_state, old["version"])
        clock.advance(30)
        store.apply_batch(reg, "stream", 2, (event(2),))
        live = next(row for row in store.listing("local", {reg.id})["items"] if row["id"] != old["id"])
        with store.transaction() as db:
            db.execute("INSERT INTO notification_retention_gaps VALUES (?,?,?,?)",
                       ("local", 1, clock(), clock() + 30))
        clock.advance(30)
        statements = []
        store.db.set_trace_callback(statements.append)
        result = store.listing("local", {reg.id})
        assert result["total"] == result["unread_count"] == 1
        assert result["items"][0]["id"] == live["id"]
        assert not result["retention"]["history_incomplete"]
        assert store.listing("local", {reg.id}, hidden=True)["total"] == 0
        assert store.get(live["id"], "local")["state"] == "unread"
        with pytest.raises(MCPFailure, match="notification_not_found"):
            store.get(old["id"], "local")
        assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
        assert store.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 2
        with pytest.raises(MCPFailure, match="notification_not_found"):
            store.set_state(old["id"], "local", "read", old["version"])
        statements.clear()
        updated = store.set_state(live["id"], "local", "read", live["version"])
        assert updated["state"] == "read" and updated["version"] == live["version"] + 1
        assert sum(sql.startswith("BEGIN") for sql in statements) == 1
        assert not any(sql.startswith("DELETE") for sql in statements)
        store.db.set_trace_callback(None)
        store.prune()
        assert store.db.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1
    finally:
        store.close()


def test_revision_limit_keeps_accepted_rows_and_does_not_recount_replay(tmp_path):
    store = NotificationStore(tmp_path / "n.db")
    reg = registration(decision="notify", kind="result",
                       match_json=encode({"task_ref": "task-fixed", "execution_ref": "run-1"}))
    store.configure(reg)
    try:
        with store.transaction() as db:
            db.executemany("INSERT INTO notification_result_revisions VALUES (?,?,?)",
                           ((reg.id, "local", digest(["task-fixed", "run-1", str(n)])) for n in range(1023)))
        batch = tuple(event(n, task_ref="task-fixed", execution_ref="run-1", revision=str(1022+n)) for n in (1, 2))
        store.apply_batch(reg, "stream", 2, batch)
        store.apply_batch(reg, "stream", 2, batch)
        result = store.listing("local", {reg.id})
        assert result["total"] == 1
        assert json.loads(result["items"][0]["metadata"])["revision"] == "1023"
        assert store.registration(reg.id)["position"] == 2
        assert store.registration(reg.id)["gap_count"] == 1
        assert store.db.execute("SELECT count(*) FROM notification_result_revisions").fetchone()[0] == 1024
    finally:
        store.close()
