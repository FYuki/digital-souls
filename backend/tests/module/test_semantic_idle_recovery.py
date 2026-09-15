"""再起動後に残ったprocessingが、UI操作なしで意味記憶処理を永久に止めない。"""

from datetime import timedelta

from app.conversation_history.models import ProcessingTurnInput
from app.memory.semantic.worker import conversation_idle
from tests.module.test_semantic_store import NOW, h as h


def test_recent_processing_blocks_but_stale_processing_does_not_require_a_ui_read(h):
    h.history.create_processing_turn("miori", h.conversation.conversation_id,
                                     ProcessingTurnInput("処理中の合成会話"))
    with h.store.source_guard.snapshot() as (history, _):
        assert not conversation_idle(history, now=NOW + timedelta(seconds=30))
        assert conversation_idle(history, now=NOW + timedelta(minutes=6))


def test_idle_gate_uses_the_configured_processing_timeout(h):
    h.history.create_processing_turn("miori", h.conversation.conversation_id,
                                     ProcessingTurnInput("処理中の合成会話"))
    with h.store.source_guard.snapshot() as (history, _):
        assert not conversation_idle(history, now=NOW + timedelta(seconds=31),
                                     stale_after=timedelta(seconds=60))
        assert conversation_idle(history, now=NOW + timedelta(seconds=31),
                                 stale_after=timedelta(seconds=30))
