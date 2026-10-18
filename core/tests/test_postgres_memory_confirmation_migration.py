"""Missing refusal guidance contract on PostgreSQL."""

import pytest

from . import test_postgres_memory_confirmation
from .memory_confirmation_contracts import Harness, complete

stores = test_postgres_memory_confirmation.stores
harness = test_postgres_memory_confirmation.harness
pytestmark = pytest.mark.postgres
BASE = "/v1/characters/synthetic/conversations"


@pytest.mark.parametrize("stream", [False, True])
def test_refusal_signal_guides_user_selected_turn_and_whole_history_deletion(
    harness: Harness, stream: bool
) -> None:
    cid = harness.conversation.create("synthetic").conversation_id
    data = complete(
        harness, cid, stream=stream, messages=[{"role": "user", "content": "覚えないで 合成発話"}]
    )
    signal = data["memory_confirmation"]
    assert signal["delete_history"] == {"method": "DELETE", "path": f"{BASE}/{cid}"}
    assert signal["delete_turns"] == {
        "method": "POST",
        "path": f"{BASE}/{cid}/turn-deletions",
        "scopes": ["selected", "following"],
    }
    snapshot = harness.http.get(f"{BASE}/{cid}").json()
    assert snapshot["revision"] == 1
    assert snapshot["private_mode"] is False
    assert [message["content"] for message in snapshot["messages"]] == [
        "覚えないで 合成発話",
        data["message"]["content"],
    ]
