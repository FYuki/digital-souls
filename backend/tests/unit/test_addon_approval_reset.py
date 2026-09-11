"""未使用単回許可の取消しと実行直前消費の順序を検証する。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.addon_action.models import ApprovalChoice, ApprovalKey, ExecutionScene, OperationGroup, Permission
from app.addon_action.store import ActionStore


def test_reset_and_dispatch_consume_serialize_without_reviving_old_answer(tmp_path):
    key = ApprovalKey("test", "identity", OperationGroup.HIGH_IMPACT, ExecutionScene.CONVERSATION)
    store = ActionStore(tmp_path / "actions.sqlite3", clock=lambda: 100)
    request = store.enqueue(key, character_id="miori", session_id="conversation", loop_id="loop",
                            fingerprint="request", preview={}, wait_seconds=60)
    store.answer(request.id, ApprovalChoice.ONCE)
    peer = ActionStore(store.path, clock=lambda: 100)
    barrier = Barrier(2)

    def reset():
        barrier.wait()
        peer.set_permission(key, Permission.UNAPPROVED)

    def consume():
        barrier.wait()
        return store.consume_request(key, request.id, 100)

    with ThreadPoolExecutor(max_workers=2) as pool:
        reset_result = pool.submit(reset)
        consumed = pool.submit(consume)
        result = consumed.result()
        reset_result.result()
    # 消費が先なら開始済み1回、取消しが先なら0回。以後の消費は常に拒否する。
    assert result in (False, True)
    assert not store.consume_request(key, request.id, 100)
    store.answer(request.id, ApprovalChoice.ONCE)
    assert not store.consume(key)
    assert store.settings(key) == {"permission": Permission.UNAPPROVED, "remaining": 0, "reserved": 0}
    assert not ActionStore(store.path).request(request.id).waiting


def test_reset_after_consumption_does_not_revoke_started_call_or_reopen_request(tmp_path):
    key = ApprovalKey("test", "identity", OperationGroup.HIGH_IMPACT, ExecutionScene.CONVERSATION)
    store = ActionStore(tmp_path / "actions.sqlite3", clock=lambda: 100)
    item = store.enqueue(key, character_id="miori", session_id="session", loop_id="loop",
                         fingerprint="request", preview={}, wait_seconds=60)
    store.answer(item.id, ApprovalChoice.ONCE)
    assert store.consume_request(key, item.id, 100)
    assert store.set_permission(key, Permission.UNAPPROVED) == ()
    store.set_permission(key, Permission.ALWAYS)
    assert not store.consume_request(key, item.id, 100)
    assert store.consume(key)
