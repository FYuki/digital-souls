"""Synthetic PostgreSQL migration of the corresponding process-local contracts."""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError
from digital_souls_core.character import AccessScope, Profile
from digital_souls_core.history import Binding, ConversationControls, SourceReference

from . import test_postgres_stores
from .conversation_support import turn
from .postgres_history_support import history
from .test_postgres_history_privacy import BINDING, policy_setup
from .test_postgres_stores import Stores

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


async def test_explicit_excluded_utterance_preserves_history_and_retry(stores: Stores) -> None:
    service, provider, _, _ = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    body = turn(
        messages=[
            {"role": "user", "content": "Synthetic excluded"},
            {"role": "user", "content": "Synthetic ordinary"},
        ],
        memory_excluded_indices=[0],
    )
    receipt = await service.complete("synthetic", cid, body)
    assert await service.complete("synthetic", cid, body) == receipt
    assert len(provider.calls) == 1
    assert "memory_excluded_indices" not in provider.calls[0][1]
    assert len(service.read("synthetic", cid).messages) == 3
    assert not service.store.source_eligible(BINDING, SourceReference(cid, 1, 0))
    assert service.store.source_eligible(BINDING, SourceReference(cid, 1, 1))
    assert not service.store.source_eligible(BINDING, SourceReference(cid, 1, 2))
    with pytest.raises(CoreError):
        await service.complete(
            "synthetic", cid, body.model_copy(update={"memory_excluded_indices": []})
        )
    reopened = history(stores)
    assert not reopened.source_eligible(BINDING, SourceReference(cid, 1, 0))
    assert reopened.source_eligible(BINDING, SourceReference(cid, 1, 1))


async def test_thread_private_then_public_does_not_retroactively_admit_private_turn(
    stores: Stores,
) -> None:
    service, _, _, _ = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    await service.complete("synthetic", cid, turn())
    assert service.store.source_eligible(BINDING, SourceReference(cid, 1, 0))
    service.controls("synthetic", cid, ConversationControls(expected_revision=1, private_mode=True))
    assert not service.store.source_eligible(BINDING, SourceReference(cid, 1, 0))
    await service.complete("synthetic", cid, turn(request_id="r2", expected_revision=2))
    assert len(service.read("synthetic", cid).messages) == 4
    assert not service.store.source_eligible(BINDING, SourceReference(cid, 3, 0))
    service.controls(
        "synthetic", cid, ConversationControls(expected_revision=3, private_mode=False)
    )
    assert service.store.source_eligible(BINDING, SourceReference(cid, 1, 0))
    assert not service.store.source_eligible(BINDING, SourceReference(cid, 3, 0))


async def test_archive_only_changes_listing_and_preserves_memory_source(stores: Stores) -> None:
    service, _, _, policy = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    await service.complete("synthetic", cid, turn())
    http = TestClient(
        create_app(service.inference, history_store=service.store, history_policy=policy),
        base_url="http://127.0.0.1",
    )
    base = "/v1/characters/synthetic/conversations"
    assert (
        http.patch(f"{base}/{cid}", json={"expected_revision": 1, "archived": True}).status_code
        == 200
    )
    assert http.get(base).json()["conversation_ids"] == []
    assert http.get(base + "?include_archived=true").json()["conversation_ids"] == [cid]
    assert http.get(f"{base}/{cid}").json()["archived"] is True
    assert service.store.source_eligible(BINDING, SourceReference(cid, 1, 0))
    assert (
        http.patch(f"{base}/{cid}", json={"expected_revision": 1, "archived": False}).status_code
        == 409
    )
    assert (
        http.patch(f"{base}/{cid}", json={"expected_revision": 2, "archived": False}).status_code
        == 200
    )
    assert service.list("synthetic") == [cid]


async def test_delete_atomically_notifies_and_invalidates_sources(stores: Stores) -> None:
    service, _, _, policy = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    await service.complete("synthetic", cid, turn())
    source = SourceReference(cid, 1, 0)
    # Synthetic stage-3 consumer only; no production memory repository exists yet.
    derived = {"synthetic-memory": {source}}
    policy.configure({})
    service.delete("synthetic", cid)
    service.delete("synthetic", cid)
    reopened = history(stores)
    events = reopened.deletions(BINDING)
    assert len(events) == 1 and events[0].through_revision == 1
    assert not reopened.source_eligible(BINDING, source)
    other = Binding(AccessScope(client="other"), "synthetic")
    assert reopened.deletions(other) == ()
    reopened.acknowledge_deletion(other, events[0].event_id)
    assert reopened.deletions(BINDING) == events
    for event in events:
        derived = {
            key: refs
            for key, refs in derived.items()
            if not any(
                ref.conversation_id == event.conversation_id
                and ref.turn_revision <= event.through_revision
                for ref in refs
            )
        }
        reopened.acknowledge_deletion(BINDING, event.event_id)
    assert derived == {} and reopened.deletions(BINDING) == ()


async def test_private_change_conflicts_with_inflight_inference(
    stores: Stores, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, provider, _, _ = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    started, release = asyncio.Event(), asyncio.Event()
    original = provider.complete

    async def wait_complete(profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await release.wait()
        return await original(profile, payload)

    monkeypatch.setattr(provider, "complete", wait_complete)
    task = asyncio.create_task(service.complete("synthetic", cid, turn()))
    await started.wait()
    service.controls("synthetic", cid, ConversationControls(expected_revision=0, private_mode=True))
    release.set()
    with pytest.raises(CoreError):
        await task
    assert service.read("synthetic", cid).messages == ()


@pytest.mark.parametrize("private", [False, True])
async def test_excluded_history_cannot_reenter_via_later_assistant(
    stores: Stores, private: bool
) -> None:
    service, provider, _, _ = policy_setup(stores)
    cid = service.create("synthetic").conversation_id
    revision = 0
    if private:
        revision = service.controls(
            "synthetic", cid, ConversationControls(expected_revision=0, private_mode=True)
        ).revision
    first = await service.complete(
        "synthetic",
        cid,
        turn(expected_revision=revision, memory_excluded_indices=[] if private else [0]),
    )
    revision = first.revision
    if private:
        revision = service.controls(
            "synthetic", cid, ConversationControls(expected_revision=revision, private_mode=False)
        ).revision
    provider.response["choices"][0]["message"]["content"] = (
        "Synthetic quotation of excluded history"
    )
    second = await service.complete(
        "synthetic", cid, turn(request_id="r2", expected_revision=revision)
    )
    assert service.store.source_eligible(BINDING, SourceReference(cid, second.revision, 0))
    assert not service.store.source_eligible(BINDING, SourceReference(cid, second.revision, 1))
    snapshot = service.read("synthetic", cid)
    assert [state.eligible for state in snapshot.memory_sources][-2:] == [True, False]
