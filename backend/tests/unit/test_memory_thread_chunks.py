from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from app.memory.formation.thread_chunks import split_thread
from app.memory.formation.thread_queue import ThreadLease, ThreadSnapshot, ThreadSource
from app.conversation_history.models import ConversationTurn, TurnStatus
from tests.conversation_history_test_support import FIXED_NOW


def snapshot(*texts):
    conversation = uuid4()
    sources = tuple(
        ThreadSource(
            ConversationTurn(
                uuid4(),
                "miori",
                conversation,
                user,
                assistant,
                TurnStatus.COMPLETED,
                None,
                FIXED_NOW,
                FIXED_NOW,
            ),
            2,
        )
        for user, assistant in texts
    )
    return ThreadSnapshot(
        ThreadLease("miori", conversation, 1, uuid4()),
        sources,
        FIXED_NOW - timedelta(days=1),
    )


@pytest.mark.parametrize("budget", [1, 7, 100, 12000])
def test_all_thread_text_is_primary_exactly_once_including_very_long_turns(budget):
    source = snapshot(
        ("前半の記憶", "応答1"),
        ("長文あいうえお" * 1000, "長い応答" * 100),
        ("最後の記憶", "応答2"),
    )
    chunks = split_thread(source, max_characters=budget, context_characters=3)
    for turn in source.sources:
        for role, text in (
            ("user", turn.turn.user_content),
            ("assistant", turn.turn.assistant_content),
        ):
            fragments = [
                p
                for chunk in chunks
                for p in chunk.primary
                if p.source == turn and p.role == role
            ]
            assert "".join(p.text for p in fragments) == text
            assert [
                position for p in fragments for position in range(p.start, p.end)
            ] == list(range(len(text)))
    for chunk in chunks:
        assert sum(len(p.text) for p in chunk.primary) <= budget
        assert sum(len(p.text) for p in chunk.fragments) <= budget + 6


def test_boundary_quote_has_one_owner_even_when_visible_in_neighbor_context():
    source = snapshot(("012旅行した。34567", "応答"))
    chunks = split_thread(source, max_characters=5, context_characters=8)
    owners = [
        c.index for c in chunks if c.owns_user_quote(source.sources[0], 3, "旅行した。")
    ]
    assert owners == [0]
    assert any(
        "旅行した。" in "".join(p.text for p in chunk.fragments) for chunk in chunks[1:]
    )
    assert not chunks[0].owns_user_quote(source.sources[0], 3, "架空の引用")


def test_repeated_same_word_at_distinct_offsets_is_not_assigned_to_wrong_chunk():
    source = snapshot(("旅行旅行旅行旅行", ""))
    chunks = split_thread(source, max_characters=4, context_characters=4)
    assert chunks[0].owns_user_quote(source.sources[0], 0, "旅行")
    assert not chunks[1].owns_user_quote(source.sources[0], 0, "旅行")
    assert chunks[1].owns_user_quote(source.sources[0], 4, "旅行")
    assert not chunks[0].owns_user_quote(source.sources[0], 4, "旅行")


def test_empty_thread_does_not_invent_a_chunk_and_key_changes_with_source_revision():
    assert split_thread(snapshot()) == ()
    first = snapshot(("旅行した", "応答"))
    changed = replace(first, sources=(replace(first.sources[0], revision=3),))
    assert split_thread(first)[0].key != split_thread(changed)[0].key


def test_recursive_bisection_covers_primary_exactly_and_shrinks_context():
    from app.memory.formation.thread_chunks import bisect_chunk

    source = snapshot(("旅行の話" * 40, "応答" * 20), ("別の出来事" * 30, "返答"))
    original = split_thread(source, max_characters=500, context_characters=100)[0]
    pending = [original]
    small = []
    while pending:
        chunk = pending.pop(0)
        if sum(p.end - p.start for p in chunk.primary) > 20:
            pending[:0] = bisect_chunk(chunk)
        else:
            small.append(chunk)
    expected = [
        (p.source.turn.turn_id, p.role, n)
        for p in original.primary
        for n in range(p.start, p.end)
    ]
    actual = [
        (p.source.turn.turn_id, p.role, n)
        for c in small
        for p in c.primary
        for n in range(p.start, p.end)
    ]
    assert actual == expected
    assert len(actual) == len(set(actual))
