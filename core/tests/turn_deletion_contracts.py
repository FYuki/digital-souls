import asyncio
import copy
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol, cast

import httpx
import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.character import AccessScope
from digital_souls_core.contracts import CompletionInput, Message
from digital_souls_core.history import Binding, SourceReference
from digital_souls_core.memory import MemoryContext

from .conversation_support import PausedProvider, turn
from .memory_confirmation_contracts import BASE, Harness, answer, complete
from .postgres_record_support import events, register_sources
from .privacy_support import BINDING
from .support import CALL, TOOL, completion


class TurnDeletionEvent(Protocol):
    event_id: str
    conversation_id: str
    turn_revisions: tuple[int, ...]


class TurnDeletionConsumer(Protocol):
    def turn_deletions(self, binding: Binding) -> tuple[TurnDeletionEvent, ...]: ...
    def acknowledge_turn_deletion(self, binding: Binding, event_id: str) -> None: ...


@dataclass
class Storage:
    harness: Harness
    reopen: Callable[[], Harness]
    query: Callable[[str, tuple[object, ...]], list[tuple[Any, ...]]]
    reject_delete: Callable[[], None]
    allow_delete: Callable[[], None]
    stored_values: Callable[[], tuple[object, ...]]


def seed(harness: Harness, texts: tuple[str, ...]) -> str:
    cid = harness.conversation.create("synthetic").conversation_id
    for revision, text in enumerate(texts):
        complete(
            harness,
            cid,
            request_id=f"r{revision + 1}",
            expected_revision=revision,
            messages=[{"role": "user", "content": text}],
        )
    return cid


def delete_turns(
    harness: Harness, cid: str, revision: int, target: int, scope: str
) -> dict[str, Any]:
    response = harness.http.post(
        f"{BASE}/{cid}/turn-deletions",
        json={"expected_revision": revision, "turn_revision": target, "scope": scope},
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json())


@pytest.mark.parametrize("scope,remaining", [("selected", (1, 3)), ("following", (1,))])
async def test_deletion_scopes_preserve_only_untargeted_history_and_memory(
    storage: Storage, scope: str, remaining: tuple[int, ...]
) -> None:
    h = storage.harness
    texts = ("Synthetic tea", "Synthetic mint", "Synthetic rose")
    cid = seed(h, texts)
    refs = tuple(SourceReference(cid, n, 0) for n in (1, 2, 3))
    memories = [register_sources(h.records, h.conversation.store, (ref,)) for ref in refs]
    result = delete_turns(h, cid, 3, 2, scope)
    assert result["conversation_id"] == cid and result["revision"] == 4
    assert set(result["turn_revisions"]) == set(range(1, 4)) - set(remaining)
    assert set(result) == {"conversation_id", "revision", "turn_revisions"}
    snapshot = h.conversation.read("synthetic", cid)
    assert snapshot.revision == 4
    assert [m.content for m in snapshot.messages if m.role == "user"] == [
        texts[n - 1] for n in remaining
    ]
    assert {s.reference.turn_revision for s in snapshot.memory_sources} == set(remaining)
    assert storage.query("SELECT revision FROM turns ORDER BY revision", ()) == [
        (n,) for n in remaining
    ]
    bodies = dict(storage.query("SELECT id,normalized_text FROM memory_episodes", ()))
    for n, old in enumerate(memories, start=1):
        assert h.records.current(BINDING, old) == (n in remaining)
        if n in remaining:
            assert bodies[old[0].identifier] == old[0].record.normalized_text
        else:
            assert bodies[old[0].identifier] is None
    assert {m.identifier for m in h.records.retrievable(BINDING)} == {
        memories[n - 1][0].identifier for n in remaining
    }


async def test_whole_conversation_deletion_preserves_existing_notification_contract(
    storage: Storage,
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic tea", "Synthetic mint", "Synthetic rose"))
    old = register_sources(h.records, h.conversation.store, (SourceReference(cid, 2, 0),))
    assert h.http.delete(f"{BASE}/{cid}").status_code == 204
    assert h.http.delete(f"{BASE}/{cid}").status_code == 204
    assert h.http.get(f"{BASE}/{cid}").status_code == 404
    assert storage.query("SELECT revision FROM turns", ()) == []
    assert storage.query("SELECT normalized_text FROM memory_episodes", ()) == [(None,)]
    assert not h.records.current(BINDING, old)
    notices = h.conversation.store.deletions(BINDING)
    assert len(notices) == 1
    assert notices[0].conversation_id == cid and notices[0].through_revision == 3


async def test_later_saved_and_new_user_turns_allow_record_registration(storage: Storage) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic tea", "Synthetic mint", "Synthetic rose"))
    delete_turns(h, cid, 3, 2, "selected")
    saved = register_sources(h.records, h.conversation.store, (SourceReference(cid, 3, 0),))
    assert json.loads(saved[0].record.normalized_text) == ["Synthetic rose"]
    complete(
        h,
        cid,
        request_id="r5",
        expected_revision=4,
        messages=[{"role": "user", "content": "Synthetic fern"}],
    )
    fresh = register_sources(h.records, h.conversation.store, (SourceReference(cid, 5, 0),))
    assert json.loads(fresh[0].record.normalized_text) == ["Synthetic fern"]
    with pytest.raises(CoreError):
        register_sources(h.records, h.conversation.store, (SourceReference(cid, 2, 0),))


@pytest.mark.parametrize("private", [False, True])
async def test_partial_deletion_does_not_admit_retained_private_or_excluded_source(
    storage: Storage, private: bool
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic removed",))
    revision = 1
    if private:
        response = h.http.patch(
            f"{BASE}/{cid}", json={"expected_revision": revision, "private_mode": True}
        )
        assert response.status_code == 200
        revision += 1
    complete(
        h,
        cid,
        request_id="retained",
        expected_revision=revision,
        memory_excluded_indices=[] if private else [0],
    )
    retained = SourceReference(cid, revision + 1, 0)
    revision += 1
    if private:
        response = h.http.patch(
            f"{BASE}/{cid}", json={"expected_revision": revision, "private_mode": False}
        )
        assert response.status_code == 200
        revision += 1
    delete_turns(h, cid, revision, 1, "selected")
    assert not h.conversation.store.source_eligible(BINDING, retained)
    with pytest.raises(CoreError):
        register_sources(h.records, h.conversation.store, (retained,))


@pytest.mark.parametrize(
    "target,scope,deleted",
    [
        (1, "selected", (1, 2, 3)),
        (2, "selected", (1, 2, 3)),
        (3, "selected", (1, 2, 3)),
        (2, "following", (1, 2, 3, 4)),
    ],
)
def test_tool_call_result_chain_expands_deletion_in_both_directions(
    storage: Storage, target: int, scope: str, deleted: tuple[int, ...]
) -> None:
    h = storage.harness
    cid = h.conversation.create("synthetic").conversation_id
    h.provider.response = completion(tool=True)
    complete(h, cid, tools=[TOOL])
    next_call = copy.deepcopy(CALL)
    next_call["id"] = "call_next"
    h.provider.response["choices"][0]["message"]["tool_calls"] = [next_call]
    complete(
        h,
        cid,
        request_id="r2",
        expected_revision=1,
        tools=[TOOL],
        messages=[
            {"role": "tool", "tool_call_id": "call_42", "content": "Synthetic first result"},
        ],
    )
    h.provider.response = completion()
    complete(
        h,
        cid,
        request_id="r3",
        expected_revision=2,
        tools=[TOOL],
        messages=[
            {"role": "tool", "tool_call_id": "call_next", "content": "Synthetic next result"},
        ],
    )
    complete(h, cid, request_id="r4", expected_revision=3)
    result = delete_turns(h, cid, 4, target, scope)
    assert set(result["turn_revisions"]) == set(deleted)
    assert {
        s.reference.turn_revision for s in h.conversation.read("synthetic", cid).memory_sources
    } == (set(range(1, 5)) - set(deleted))
    complete(h, cid, request_id="r6", expected_revision=5)


@pytest.mark.parametrize(
    "text",
    [
        "call_42",
        '> "call_42"',
        "`call_42`",
        '```json\n{"tool_call_id":"call_42"}\n```',
        "~~~text\ncall_42\n~~~",
        '```json\n{"nested":{"tool_call_id":"call_42"}}',
    ],
)
def test_tool_identifiers_in_content_do_not_expand_deletion(storage: Storage, text: str) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic first", text, "Synthetic last"))
    delete_turns(h, cid, 3, 2, "selected")
    assert {
        s.reference.turn_revision for s in h.conversation.read("synthetic", cid).memory_sources
    } == {1, 3}


@pytest.mark.parametrize("changed", [False, True])
def test_deleted_request_retry_is_rejected_after_restart(storage: Storage, changed: bool) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic tea", "Synthetic mint", "Synthetic rose"))
    delete_turns(h, cid, 3, 2, "selected")
    restored = storage.reopen()
    with restored.http:
        before = restored.conversation.read("synthetic", cid)
        body = turn(
            request_id="r2",
            expected_revision=4 if changed else 1,
            messages=[
                {"role": "user", "content": "Synthetic changed" if changed else "Synthetic mint"},
            ],
        )
        response = restored.http.post(f"{BASE}/{cid}/completions", json=body.model_dump())
        assert response.status_code == 409
        assert restored.provider.calls == []
        assert restored.conversation.read("synthetic", cid) == before
        with pytest.raises(CoreError) as error:
            restored.conversation.store.receipt(BINDING, cid, "r2")
        assert error.value.status == 409
        with pytest.raises(CoreError) as error:
            restored.conversation.store.append(
                BINDING,
                cid,
                "r2",
                "synthetic-new-fingerprint",
                4,
                (*body.messages, Message(role="assistant", content="Synthetic new reply")),
                "stop",
            )
        assert error.value.status == 409
        assert restored.conversation.read("synthetic", cid) == before


@pytest.mark.parametrize("stream", [False, True])
async def test_partial_deletion_rejects_same_inflight_http_completion(
    storage: Storage, stream: bool
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic tea", "Synthetic mint", "Synthetic rose"))
    provider = PausedProvider()
    h.conversation.inference.provider = provider
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.http.app), base_url="http://127.0.0.1"
    ) as http:
        task = asyncio.create_task(
            http.post(
                f"{BASE}/{cid}/completions",
                json=turn(request_id="r4", expected_revision=3, stream=stream).model_dump(
                    exclude_none=True
                ),
            )
        )
        try:
            await asyncio.wait_for(provider.started.wait(), 5)
            assert not task.done()
            assert h.conversation.read("synthetic", cid).revision == 3
            deletion = await http.post(
                f"{BASE}/{cid}/turn-deletions",
                json={"expected_revision": 3, "turn_revision": 2, "scope": "selected"},
            )
            assert deletion.status_code == 200
            assert deletion.json() == {"conversation_id": cid, "revision": 4, "turn_revisions": [2]}
            after_delete = h.conversation.read("synthetic", cid)
            assert {s.reference.turn_revision for s in after_delete.memory_sources} == {1, 3}
        finally:
            provider.release.set()
            result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    response = result[0]
    assert isinstance(response, httpx.Response)
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["type"] == "core_error"
    assert response.json()["error"]["code"] == "revision_conflict"
    assert h.conversation.read("synthetic", cid) == after_delete
    assert h.conversation.store.receipt(BINDING, cid, "r4") is None
    assert storage.query("SELECT revision FROM turns ORDER BY revision", ()) == [(1,), (3,)]
    if stream:
        assert provider.closed


@pytest.mark.parametrize("stream", [False, True])
async def test_partial_deletion_rejects_same_inflight_completion(
    storage: Storage, stream: bool
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic tea",))
    provider = PausedProvider()
    h.conversation.inference.provider = provider
    task = asyncio.create_task(
        h.conversation.complete(
            "synthetic",
            cid,
            turn(request_id="r2", expected_revision=1, stream=stream),
        )
    )
    try:
        await asyncio.wait_for(provider.started.wait(), 5)
        delete_turns(h, cid, 1, 1, "selected")
    finally:
        provider.release.set()
        result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], CoreError) and result[0].status == 409
    assert h.conversation.read("synthetic", cid).revision == 2
    assert h.conversation.store.receipt(BINDING, cid, "r2") is None
    if stream:
        assert provider.closed


async def test_partial_deletion_invalidates_same_prepared_memory_context(storage: Storage) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic tea", "Synthetic rose"))
    inference = h.conversation.inference
    from dataclasses import replace

    from digital_souls_core.memory_contracts import SourceVersion
    from digital_souls_core.memory_record_store import RecordBatch
    from digital_souls_core.memory_records import Citation, Speaker
    from digital_souls_core.memory_retrieval import MemoryRetrieval

    from .record_retrieval_support import SyntheticEmbedding
    from .test_memory_record_store_contract import episode

    records = h.records
    c = Citation(BINDING, SourceVersion(SourceReference(cid, 1, 0), 0), Speaker.USER, 0, 5)
    records.register(
        BINDING,
        RecordBatch(episodes=(replace(episode(c), normalized_text="Synthetic tea experience"),)),
        "fixture-v1",
    )
    inference.memory_context = MemoryContext(
        MemoryRetrieval(records, h.policy, embedding=SyntheticEmbedding())
    )
    prepared = await inference.prepare(
        "synthetic",
        CompletionInput(messages=[Message(role="user", content="tea")]),
        alias=False,
        conversation_id=cid,
    )
    inference.check(prepared)
    data = json.loads(prepared.payload["messages"][1]["content"].split("\n", 1)[1])
    assert data[0]["text"] == "Synthetic tea experience"
    calls = len(h.provider.calls)
    delete_turns(h, cid, 2, 1, "selected")
    with pytest.raises(CoreError):
        inference.check(prepared)
    assert len(h.provider.calls) == calls


def test_remaining_dates_confirmation_states_and_receipts_survive_restart(storage: Storage) -> None:
    h = storage.harness
    cid = seed(
        h, ("Synthetic removed", "覚えないで Synthetic pending", "記録しないで Synthetic answered")
    )
    answer(h, cid, 3, 0, False, turn_revision=3)
    storage.query("UPDATE turns SET stated_at=NULL WHERE revision=2", ())
    before = h.conversation.read("synthetic", cid)
    receipts = {
        request: h.conversation.store.receipt(BINDING, cid, request) for request in ("r2", "r3")
    }
    saved = storage.query(
        "SELECT revision,stated_at,memory_confirmation,fingerprint FROM turns "
        "WHERE revision>1 ORDER BY revision",
        (),
    )
    delete_turns(h, cid, 4, 1, "selected")
    restored = storage.reopen()
    with restored.http:
        after = restored.conversation.read("synthetic", cid)
        assert after.messages == before.messages[2:]
        assert after.memory_sources == before.memory_sources[2:]
        assert after.memory_confirmations == before.memory_confirmations
        assert (
            storage.query(
                "SELECT revision,stated_at,memory_confirmation,fingerprint FROM turns "
                "ORDER BY revision",
                (),
            )
            == saved
        )
        for request, receipt in receipts.items():
            assert restored.conversation.store.receipt(BINDING, cid, request) == receipt
        assert not restored.conversation.store.source_eligible(BINDING, SourceReference(cid, 2, 0))
        assert restored.conversation.store.source_eligible(BINDING, SourceReference(cid, 3, 0))


def test_partial_notification_is_durable_scoped_and_contains_only_identifiers(
    storage: Storage,
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic tea", "Synthetic mint", "Synthetic rose"))
    delete_turns(h, cid, 3, 2, "selected")
    restored = storage.reopen()
    consumer = cast(TurnDeletionConsumer, restored.conversation.store)
    notices = consumer.turn_deletions(BINDING)
    assert len(notices) == 1
    notice = notices[0]
    assert notice.conversation_id == cid and tuple(notice.turn_revisions) == (2,)
    assert set(asdict(cast(Any, notice))) == {"event_id", "conversation_id", "turn_revisions"}
    assert restored.conversation.store.deletions(BINDING) == ()
    other = Binding(AccessScope(client="synthetic-other"), "synthetic")
    assert consumer.turn_deletions(other) == ()
    consumer.acknowledge_turn_deletion(other, notice.event_id)
    assert consumer.turn_deletions(BINDING) == notices
    consumer.acknowledge_turn_deletion(BINDING, notice.event_id)
    consumer.acknowledge_turn_deletion(BINDING, notice.event_id)
    assert consumer.turn_deletions(BINDING) == ()


def test_retained_accepted_confirmation_stays_ineligible_after_partial_delete(
    storage: Storage,
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic removed", "覚えないで Synthetic accepted"))
    answer(h, cid, 2, 0, True, turn_revision=2)
    response = h.http.patch(f"{BASE}/{cid}", json={"expected_revision": 3, "private_mode": False})
    assert response.status_code == 200
    before = h.conversation.read("synthetic", cid)
    receipt = h.conversation.store.receipt(BINDING, cid, "r2")
    delete_turns(h, cid, 4, 1, "selected")
    restored = storage.reopen()
    with restored.http:
        after = restored.conversation.read("synthetic", cid)
        assert after.messages == before.messages[2:]
        assert after.memory_sources == before.memory_sources[2:]
        assert after.memory_confirmations == ()
        assert restored.conversation.store.receipt(BINDING, cid, "r2") == receipt
        assert not restored.conversation.store.source_eligible(BINDING, SourceReference(cid, 2, 0))
        state = storage.query("SELECT memory_confirmation FROM turns WHERE revision=2", ())
        assert json.loads(state[0][0]) == {"0": True}


@pytest.mark.parametrize("revision,target,status", [(2, 2, 409), (3, 99, 409)])
def test_invalid_deletion_leaves_all_state_unchanged(
    storage: Storage,
    revision: int,
    target: int,
    status: int,
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic tea", "Synthetic mint", "Synthetic rose"))
    before = h.conversation.read("synthetic", cid)
    response = h.http.post(
        f"{BASE}/{cid}/turn-deletions",
        json={
            "expected_revision": revision,
            "turn_revision": target,
            "scope": "selected",
        },
    )
    assert response.status_code == status
    assert h.conversation.read("synthetic", cid) == before
    assert events(h.records) == ()


def test_partial_deletion_remains_available_after_consent_revocation(storage: Storage) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic tea", "Synthetic rose"))
    h.policy.configure({})
    delete_turns(h, cid, 2, 1, "selected")
    assert {
        s.reference.turn_revision for s in h.conversation.store.read(BINDING, cid).memory_sources
    } == {2}


async def test_failed_delete_rolls_back_revision_history_memory_and_allows_retry(
    storage: Storage,
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic tea", "Synthetic rose"))
    old = register_sources(h.records, h.conversation.store, (SourceReference(cid, 1, 0),))
    before = h.conversation.read("synthetic", cid)
    receipt = h.conversation.store.receipt(BINDING, cid, "r1")
    storage.reject_delete()
    try:
        response = h.http.post(
            f"{BASE}/{cid}/turn-deletions",
            json={
                "expected_revision": 2,
                "turn_revision": 1,
                "scope": "selected",
            },
        )
        assert response.status_code >= 500
        assert h.conversation.read("synthetic", cid) == before
        assert h.conversation.store.receipt(BINDING, cid, "r1") == receipt
        assert h.records.current(BINDING, old)
        assert events(h.records) == ()
        assert cast(TurnDeletionConsumer, h.conversation.store).turn_deletions(BINDING) == ()
        body = storage.query("SELECT normalized_text FROM memory_episodes", ())[0][0]
        assert body == old[0].record.normalized_text
    finally:
        storage.allow_delete()
    delete_turns(h, cid, 2, 1, "selected")
    assert storage.query("SELECT revision FROM turns", ()) == [(2,)]
    assert storage.query("SELECT normalized_text FROM memory_episodes", ()) == [(None,)]


def test_natural_language_does_not_delete_history(storage: Storage) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic first", "Delete the previous turn", "Synthetic last"))
    assert {
        s.reference.turn_revision for s in h.conversation.read("synthetic", cid).memory_sources
    } == {1, 2, 3}


def test_deleted_tail_revision_is_never_reused(storage: Storage) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic first",))
    for revision in (1, 3):
        response = h.http.patch(
            f"{BASE}/{cid}", json={"expected_revision": revision, "archived": False}
        )
        assert response.status_code == 200
        complete(h, cid, request_id=f"r{revision + 2}", expected_revision=revision + 1)
    assert storage.query("SELECT revision FROM turns ORDER BY revision", ()) == [(1,), (3,), (5,)]
    delete_turns(h, cid, 5, 5, "selected")
    complete(h, cid, request_id="r7", expected_revision=6)
    assert storage.query("SELECT revision FROM turns ORDER BY revision", ()) == [(1,), (3,), (7,)]
    assert {
        state.reference.turn_revision
        for state in h.conversation.read("synthetic", cid).memory_sources
    } == {1, 3, 7}


def test_same_tool_name_with_different_ids_does_not_join_independent_turns(
    storage: Storage,
) -> None:
    h = storage.harness
    cid = h.conversation.create("synthetic").conversation_id
    for offset, identifier in ((0, "call_first"), (2, "call_second")):
        response = completion(tool=True)
        response["choices"][0]["message"]["tool_calls"][0]["id"] = identifier
        h.provider.response = response
        complete(h, cid, request_id=f"r{offset + 1}", expected_revision=offset, tools=[TOOL])
        h.provider.response = completion()
        complete(
            h,
            cid,
            request_id=f"r{offset + 2}",
            expected_revision=offset + 1,
            tools=[TOOL],
            messages=[
                {"role": "tool", "tool_call_id": identifier, "content": "Synthetic result"},
            ],
        )
    delete_turns(h, cid, 4, 1, "selected")
    assert {
        s.reference.turn_revision for s in h.conversation.read("synthetic", cid).memory_sources
    } == {3, 4}
    complete(h, cid, request_id="r6", expected_revision=5, tools=[TOOL])


def test_multiple_calls_and_results_are_deleted_together(storage: Storage) -> None:
    h = storage.harness
    cid = h.conversation.create("synthetic").conversation_id
    response = completion(tool=True)
    second = copy.deepcopy(CALL)
    second["id"] = "call_second"
    response["choices"][0]["message"]["tool_calls"].append(second)
    h.provider.response = response
    complete(h, cid, tools=[TOOL])
    h.provider.response = completion()
    complete(
        h,
        cid,
        request_id="r2",
        expected_revision=1,
        tools=[TOOL],
        messages=[
            {"role": "tool", "tool_call_id": identifier, "content": "Synthetic result"}
            for identifier in ("call_second", "call_42")
        ],
    )
    complete(h, cid, request_id="r3", expected_revision=2)
    result = delete_turns(h, cid, 3, 2, "selected")
    assert set(result["turn_revisions"]) == {1, 2}
    assert {
        s.reference.turn_revision for s in h.conversation.read("synthetic", cid).memory_sources
    } == {3}


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": "all"},
        {"scope": None},
        {"turn_revision": 0},
        {"expected_revision": -1},
        {"client": "synthetic-other"},
    ],
)
def test_invalid_deletion_input_is_rejected_without_changes(
    storage: Storage, changes: dict[str, object]
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic first",))
    before = h.conversation.read("synthetic", cid)
    response = h.http.post(
        f"{BASE}/{cid}/turn-deletions",
        json={
            "expected_revision": 1,
            "turn_revision": 1,
            "scope": "selected",
            **changes,
        },
    )
    assert response.status_code == 400
    assert h.conversation.read("synthetic", cid) == before


def test_partial_deletion_cannot_change_another_binding(storage: Storage) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic first",))
    before = h.conversation.store.read(BINDING, cid)
    h.conversation.inference.scope = AccessScope(client="synthetic-other")
    response = h.http.post(
        f"{BASE}/{cid}/turn-deletions",
        json={
            "expected_revision": 1,
            "turn_revision": 1,
            "scope": "selected",
        },
    )
    assert response.status_code == 404
    assert h.conversation.store.read(BINDING, cid) == before


def test_repeated_partial_deletion_is_rejected_without_second_revision_change(
    storage: Storage,
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic first", "Synthetic last"))
    delete_turns(h, cid, 2, 1, "selected")
    before = h.conversation.read("synthetic", cid)
    response = h.http.post(
        f"{BASE}/{cid}/turn-deletions",
        json={
            "expected_revision": 3,
            "turn_revision": 1,
            "scope": "selected",
        },
    )
    assert response.status_code == 409
    assert h.conversation.read("synthetic", cid) == before


async def test_tombstone_keeps_request_identifier_without_content_or_fingerprint(
    storage: Storage,
) -> None:
    h = storage.harness
    cid = seed(h, ("Synthetic kept rose", "Synthetic removed cactus"))
    receipt = h.conversation.store.receipt(BINDING, cid, "r2")
    assert receipt is not None
    register_sources(h.records, h.conversation.store, (SourceReference(cid, 2, 0),))
    delete_turns(h, cid, 2, 2, "selected")
    values = storage.stored_values()
    assert "r2" in values
    assert all("cactus" not in str(value).casefold() for value in values)
    assert all(receipt.fingerprint not in str(value) for value in values)
    assert h.http.delete(f"{BASE}/{cid}").status_code == 204
    assert "r2" not in storage.stored_values()
