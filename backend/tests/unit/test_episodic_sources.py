from datetime import timedelta
import sqlite3
from uuid import uuid4

import pytest

from app.conversation_history.models import ProcessingTurnInput
from app.memory.episodic.contracts import SourceSpan
from app.memory.episodic.sources import ConversationSourceGuard, InvalidConversationSource
from tests.conversation_history_test_support import create_repository, FIXED_NOW


def setup(tmp_path):
    path = tmp_path / "conversation.db"
    repository = create_repository(path, uuid_factory=uuid4)
    conversation = repository.create_conversation("miori")
    turn = repository.create_processing_turn("miori", conversation.conversation_id,
                                             ProcessingTurnInput("昨日うどんを食べた"))
    turn = repository.complete_turn("miori", conversation.conversation_id, turn.turn_id,
                                     sanitized_assistant_content="駅前で昼食を食べたのですね")
    guard = ConversationSourceGuard(path, clock=lambda: FIXED_NOW, retention=timedelta(days=30))
    span = SourceSpan(source_id=turn.turn_id, revision=2, role="user", start=0,
                       end=len(turn.user_content), stated_at=turn.created_at)
    return path, conversation, turn, guard, span


def test_current_turn_source_and_assistant_timestamp_are_accepted(tmp_path):
    _, conversation, turn, guard, span = setup(tmp_path)
    assistant = SourceSpan(source_id=turn.turn_id, revision=2, role="assistant", start=0,
                            end=len(turn.assistant_content), stated_at=turn.updated_at)
    with guard.guard(character_id="miori", conversation_id=conversation.conversation_id,
                     sources=(span, assistant)):
        pass


@pytest.mark.parametrize("change", ["character", "thread", "revision", "range", "time", "role"])
def test_fabricated_source_scope_range_or_timestamp_is_rejected(tmp_path, change):
    _, conversation, _, guard, span = setup(tmp_path)
    if change == "revision":
        span = span.model_copy(update={"revision": 1})
    elif change == "range":
        span = span.model_copy(update={"end": 10000})
    elif change == "time":
        span = span.model_copy(update={"stated_at": span.stated_at + timedelta(seconds=1)})
    elif change == "role":
        span = span.model_copy(update={"role": "manual"})
    with pytest.raises(InvalidConversationSource):
        with guard.guard(character_id="other" if change == "character" else "miori",
                         conversation_id=uuid4() if change == "thread" else conversation.conversation_id,
                         sources=(span,)):
            pytest.fail("invalid source must not reach the write transaction")


@pytest.mark.parametrize("change", ["edit", "delete", "screen"])
def test_source_guard_rejects_current_history_invalidation(tmp_path, change):
    path, conversation, turn, guard, span = setup(tmp_path)
    with sqlite3.connect(path) as connection:
        if change == "edit":
            connection.execute("UPDATE conversation_turns SET user_content = '訂正文' WHERE turn_id = ?",
                               (str(turn.turn_id),))
        elif change == "delete":
            connection.execute("DELETE FROM conversation_turns WHERE turn_id = ?", (str(turn.turn_id),))
        else:
            connection.execute(
                "INSERT INTO screen_turn_provenance VALUES(?, ?, ?, 1, 'route', 'explicit_ui', 'window', 'direct_observation')",
                (str(turn.turn_id), str(uuid4()), str(uuid4())))
    with pytest.raises(InvalidConversationSource):
        with guard.guard(character_id="miori", conversation_id=conversation.conversation_id, sources=(span,)):
            pytest.fail("changed source must not reach memory")


def test_expired_source_cannot_be_reused(tmp_path):
    path, conversation, _, _, span = setup(tmp_path)
    guard = ConversationSourceGuard(path, clock=lambda: FIXED_NOW + timedelta(days=31),
                                    retention=timedelta(days=30))
    with pytest.raises(InvalidConversationSource):
        with guard.guard(character_id="miori", conversation_id=conversation.conversation_id, sources=(span,)):
            pytest.fail("expired source")


def test_guard_holds_history_stable_until_memory_transaction_finishes(tmp_path):
    path, conversation, turn, guard, span = setup(tmp_path)
    with guard.guard(character_id="miori", conversation_id=conversation.conversation_id, sources=(span,)):
        with sqlite3.connect(path, timeout=0) as competing:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competing.execute("UPDATE conversation_turns SET user_content = '変更' WHERE turn_id = ?",
                                  (str(turn.turn_id),))
    with sqlite3.connect(path) as competing:
        competing.execute("UPDATE conversation_turns SET user_content = '変更' WHERE turn_id = ?",
                          (str(turn.turn_id),))
    with pytest.raises(InvalidConversationSource):
        with guard.guard(character_id="miori", conversation_id=conversation.conversation_id, sources=(span,)):
            pytest.fail("source revision changed after guard released")
