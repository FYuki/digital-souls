"""Backend-independent canonical-record contracts, run through the storage port."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.character import AccessScope
from digital_souls_core.contracts import Message
from digital_souls_core.history import (
    Binding,
    ConversationControls,
    MemoryConfirmation,
    SourceReference,
    TurnDeletionInput,
)
from digital_souls_core.memory_contracts import SourceVersion
from digital_souls_core.memory_records import (
    Citation,
    Episode,
    EpisodeContext,
    EpisodeEvidence,
    EpisodeFactLink,
    ExplicitReason,
    Fact,
    FiveW,
    FormationType,
    Proposition,
    RecordKind,
    RecordRef,
    RecordState,
    Semantic,
    Speaker,
    TemporalValue,
)

from . import test_postgres_stores
from .test_postgres_stores import BINDING, Stores, seed

if TYPE_CHECKING:
    from digital_souls_core.memory_record_store import MemoryRecordStore

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores
NOW = datetime(2026, 10, 7, tzinfo=UTC)
OTHER = Binding(AccessScope(subject="other"), "synthetic")


@pytest.fixture
def port(stores: Stores) -> "MemoryRecordStore":
    from digital_souls_core.postgres_memory_records import PostgresMemoryRecords

    return PostgresMemoryRecords(stores.database)


def citation(stores: Stores) -> Citation:
    return Citation(BINDING, SourceVersion(seed(stores), 0), Speaker.USER, 0, 10)


def episode(c: Citation, identifier: str = "e1") -> Episode:
    return Episode(
        episode_id=identifier,
        version=1,
        binding=BINDING,
        normalized_text="Synthetic experience",
        created_at=NOW,
        last_user_mentioned_at=None,
        state=RecordState.ACTIVE,
        five_w=FiveW(predicate="heard", why=ExplicitReason("explicit", (c,))),
        experience_time=TemporalValue(),
        experienced_at=None,
        context=EpisodeContext.ACTUAL,
        citations=(c,),
    )


def fact(c: Citation) -> Fact:
    return Fact(
        fact_id="f1",
        version=1,
        binding=BINDING,
        normalized_text="Synthetic fact",
        created_at=NOW,
        last_user_mentioned_at=NOW,
        state=RecordState.ACTIVE,
        five_w=FiveW(predicate="likes", object="tea"),
        target_time=TemporalValue(),
        citations=(c,),
    )


def reference(record: Episode | Fact | Semantic | EpisodeFactLink) -> RecordRef:
    kind, identifier = (
        (RecordKind.EPISODE, record.episode_id)
        if isinstance(record, Episode)
        else (RecordKind.FACT, record.fact_id)
        if isinstance(record, Fact)
        else (RecordKind.SEMANTIC, record.semantic_id)
        if isinstance(record, Semantic)
        else (RecordKind.EPISODE_FACT_LINK, record.link_id)
    )
    return RecordRef(
        kind=kind, record_id=identifier, version=record.version, binding=record.binding
    )


def derived(first: Episode, second: Episode) -> Semantic:
    return Semantic(
        semantic_id="s1",
        version=1,
        binding=BINDING,
        normalized_text="Synthetic tendency",
        created_at=NOW,
        last_user_mentioned_at=None,
        state=RecordState.ACTIVE,
        formation_type=FormationType.EXPERIENCE_DERIVED,
        proposition=Proposition(subject="synthetic", attribute="likes", value="tea"),
        applicability=TemporalValue(),
        citations=(),
        episode_evidence=tuple(
            EpisodeEvidence(
                reference=reference(e), sources=frozenset(c.source.reference for c in e.citations)
            )
            for e in (first, second)
        ),
    )


def test_atomic_roundtrip_and_active_binding_reads(
    port: "MemoryRecordStore", stores: Stores
) -> None:
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch

    e = episode(citation(stores))
    e2 = episode(citation(stores), "e2")
    f = fact(e.citations[0])
    s = derived(e, e2)
    link = EpisodeFactLink(
        link_id="l1",
        version=1,
        binding=BINDING,
        created_at=NOW,
        state=RecordState.ACTIVE,
        episode=reference(e),
        fact=reference(f),
    )
    batch = RecordBatch(episodes=(e, e2), facts=(FactWrite(f),), links=(link,), semantics=(s,))
    result = port.register(BINDING, batch, "formation-v1")
    assert set(result) == {reference(r) for r in (e, e2, f, s, link)}
    for r in (e, e2, f, s, link):
        ref = reference(r)
        assert port.get(BINDING, ref.kind, ref.record_id) == r
        assert r in port.list(BINDING, ref.kind)
        assert port.get(OTHER, ref.kind, ref.record_id) is None
        assert port.list(OTHER, ref.kind) == ()
    assert port.register(BINDING, batch, "formation-v1") == result


@pytest.fixture
def link_pair(port: "MemoryRecordStore", stores: Stores) -> tuple[EpisodeFactLink, EpisodeFactLink]:
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch

    e, e2 = episode(citation(stores)), episode(citation(stores), "e2")
    f, f2 = fact(e.citations[0]), replace(fact(e2.citations[0]), fact_id="f2")
    port.register(
        BINDING,
        RecordBatch(episodes=(e, e2), facts=(FactWrite(f), FactWrite(f2))),
        "targets-v1",
    )
    links = tuple(
        EpisodeFactLink(
            link_id=identifier,
            version=1,
            binding=BINDING,
            created_at=NOW,
            state=RecordState.ACTIVE,
            episode=reference(target_episode),
            fact=reference(target_fact),
        )
        for identifier, target_episode, target_fact in (("l1", e, f), ("l2", e2, f2))
    )
    return links[0], links[1]


@pytest.mark.parametrize("changed_endpoint", ["episode", "fact"])
def test_link_only_registrations_distinguish_endpoints(
    port: "MemoryRecordStore",
    link_pair: tuple[EpisodeFactLink, EpisodeFactLink],
    changed_endpoint: str,
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    first, second = link_pair
    second = (
        replace(second, fact=first.fact)
        if changed_endpoint == "episode"
        else replace(second, episode=first.episode)
    )
    for link in (first, second):
        assert port.register(BINDING, RecordBatch(links=(link,)), "links-v1") == (reference(link),)
    for link in (first, second):
        assert port.get(BINDING, RecordKind.EPISODE_FACT_LINK, link.link_id) == link
    assert set(port.list(BINDING, RecordKind.EPISODE_FACT_LINK)) == {first, second}


def test_link_only_registrations_distinguish_endpoint_pairs(
    port: "MemoryRecordStore", link_pair: tuple[EpisodeFactLink, EpisodeFactLink]
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    first, second = link_pair
    crossed_first = replace(first, link_id="l3", fact=second.fact)
    crossed_second = replace(second, link_id="l4", fact=first.fact)
    for links in ((first, second), (crossed_first, crossed_second)):
        result = port.register(BINDING, RecordBatch(links=links), "links-v1")
        assert set(result) == {reference(link) for link in links}
    all_links = (first, second, crossed_first, crossed_second)
    for link in all_links:
        assert port.get(BINDING, RecordKind.EPISODE_FACT_LINK, link.link_id) == link
    assert set(port.list(BINDING, RecordKind.EPISODE_FACT_LINK)) == set(all_links)


def test_link_only_registration_retry_returns_existing_result(
    port: "MemoryRecordStore", link_pair: tuple[EpisodeFactLink, EpisodeFactLink]
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    batch = RecordBatch(links=(link_pair[0],))
    result = port.register(BINDING, batch, "links-v1")
    assert port.register(BINDING, batch, "links-v1") == result == (reference(link_pair[0]),)
    assert port.list(BINDING, RecordKind.EPISODE_FACT_LINK) == (link_pair[0],)


def test_link_only_registration_normalizes_shared_endpoints_and_order(
    port: "MemoryRecordStore", link_pair: tuple[EpisodeFactLink, EpisodeFactLink]
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    first, second = link_pair
    second = replace(second, fact=first.fact)
    result = port.register(BINDING, RecordBatch(links=(first, second)), "links-v1")
    assert port.register(BINDING, RecordBatch(links=(second, first)), "links-v1") == result
    assert set(result) == {reference(first), reference(second)}


def test_link_only_registration_after_another_link_is_revoked(
    port: "MemoryRecordStore",
    stores: Stores,
    link_pair: tuple[EpisodeFactLink, EpisodeFactLink],
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    first, second = link_pair
    batch = RecordBatch(links=(first,))
    port.register(BINDING, batch, "links-v1")
    e = port.get(BINDING, RecordKind.EPISODE, first.episode.record_id)
    assert isinstance(e, Episode)
    stores.history.delete(BINDING, e.citations[0].source.reference.conversation_id)
    head = port.head(BINDING, RecordKind.EPISODE_FACT_LINK, first.link_id)
    assert head is not None and head.state is RecordState.SUSPENDED
    with pytest.raises(CoreError) as error:
        port.register(BINDING, batch, "links-v1")
    assert error.value.status == 409
    assert error.value.code == "memory_registration_conflict"
    assert port.register(BINDING, RecordBatch(links=(second,)), "links-v1") == (reference(second),)
    assert port.get(BINDING, RecordKind.EPISODE_FACT_LINK, first.link_id) is None
    assert port.get(BINDING, RecordKind.EPISODE_FACT_LINK, second.link_id) == second


def test_atomic_failure_after_valid_episode(port: "MemoryRecordStore", stores: Stores) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    e, e2 = episode(citation(stores)), episode(citation(stores), "missing")
    s = derived(e, e2)
    with pytest.raises(CoreError):
        port.register(BINDING, RecordBatch(episodes=(e,), semantics=(s,)), "v1")
    assert port.list(BINDING, RecordKind.EPISODE) == ()
    assert port.list(BINDING, RecordKind.SEMANTIC) == ()
    port.register(BINDING, RecordBatch(episodes=(e, e2), semantics=(s,)), "v1")


@pytest.mark.parametrize(
    "invalid",
    [
        "epoch",
        "revision",
        "index",
        "bounds",
        "speaker",
        "excluded",
        "pending",
        "private",
        "deleted",
        "turn_deleted",
        "binding",
    ],
)
def test_current_source_citation_validation(
    port: "MemoryRecordStore", stores: Stores, invalid: str
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    c = citation(stores)
    ref = c.source.reference
    if invalid in {"excluded", "pending"}:
        cid = stores.history.create(BINDING).conversation_id
        stores.history.append(
            BINDING,
            cid,
            "r1",
            "fp1",
            0,
            (
                Message(role="user", content="Synthetic input"),
                Message(role="assistant", content="answer"),
            ),
            "stop",
            memory_excluded_indices=(0,) if invalid == "excluded" else (),
            memory_confirmation_indices=(0,) if invalid == "pending" else (),
        )
        c = replace(c, source=SourceVersion(SourceReference(cid, 1, 0), 0))
    elif invalid == "private":
        stores.history.controls(
            BINDING,
            ref.conversation_id,
            ConversationControls(expected_revision=1, private_mode=True),
        )
    elif invalid == "deleted":
        stores.history.delete(BINDING, ref.conversation_id)
    elif invalid == "turn_deleted":
        stores.history.delete_turns(
            BINDING,
            ref.conversation_id,
            TurnDeletionInput(expected_revision=1, turn_revision=1, scope="selected"),
        )
    elif invalid == "epoch":
        c = replace(c, source=SourceVersion(ref, 1))
    elif invalid in {"revision", "index"}:
        c = replace(
            c,
            source=SourceVersion(
                replace(ref, turn_revision=99)
                if invalid == "revision"
                else replace(ref, message_index=99),
                0,
            ),
        )
    elif invalid == "bounds":
        c = replace(c, end=999)
    elif invalid == "speaker":
        c = replace(c, speaker=Speaker.ASSISTANT)
    binding = OTHER if invalid == "binding" else BINDING
    with pytest.raises(CoreError):
        port.register(binding, RecordBatch(episodes=(episode(c),)), "v1")
    assert port.list(BINDING, RecordKind.EPISODE) == ()


def test_explicit_reason_citations_are_validated(port: "MemoryRecordStore", stores: Stores) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    c = citation(stores)
    stale = replace(c, source=SourceVersion(c.source.reference, 99))
    e = replace(episode(c), five_w=FiveW(predicate="heard", why=ExplicitReason("reason", (stale,))))
    with pytest.raises(CoreError):
        port.register(BINDING, RecordBatch(episodes=(e,)), "v1")


def test_confirmation_declined_and_assistant_quote(
    port: "MemoryRecordStore", stores: Stores
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    cid = stores.history.create(BINDING).conversation_id
    stores.history.append(
        BINDING,
        cid,
        "r",
        "fp",
        0,
        (
            Message(role="user", content="Synthetic input"),
            Message(role="assistant", content="Synthetic answer"),
        ),
        "stop",
        memory_confirmation_indices=(0,),
    )
    stores.history.confirm(
        BINDING,
        cid,
        MemoryConfirmation(
            expected_revision=1, turn_revision=1, message_index=0, accept_private_mode=False
        ),
    )
    c = Citation(BINDING, SourceVersion(SourceReference(cid, 1, 0), 0), Speaker.USER, 0, 9)
    port.register(BINDING, RecordBatch(episodes=(episode(c),)), "v1")
    # Independent assistant quote is allowed when history marks it eligible.
    c2 = citation(stores)
    c2 = replace(
        c2,
        source=SourceVersion(replace(c2.source.reference, message_index=1), 0),
        speaker=Speaker.ASSISTANT,
    )
    port.register(BINDING, RecordBatch(episodes=(episode(c2, "e2"),)), "v1")


def test_idempotent_set_normalization_and_conflict(
    port: "MemoryRecordStore", stores: Stores
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    c, c2 = citation(stores), citation(stores)
    e = replace(episode(c), citations=(c, c2))
    batch = RecordBatch(episodes=(e,))
    result = port.register(BINDING, batch, "v1")
    assert (
        port.register(BINDING, RecordBatch(episodes=(replace(e, citations=(c2, c, c)),)), "v1")
        == result
    )
    with pytest.raises(CoreError) as error:
        port.register(BINDING, RecordBatch(episodes=(replace(e, normalized_text="changed"),)), "v1")
    assert error.value.status == 409
    assert port.list(BINDING, RecordKind.EPISODE) == (e,)
    with pytest.raises(CoreError):
        port.register(BINDING, batch, "different-version")


def test_fact_cas_old_versions_and_retry(port: "MemoryRecordStore", stores: Stores) -> None:
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch

    f = fact(citation(stores))
    port.register(BINDING, RecordBatch(facts=(FactWrite(f),)), "v1")
    f2 = replace(
        f, version=2, normalized_text="Synthetic revised fact", citations=(citation(stores),)
    )
    batch = RecordBatch(facts=(FactWrite(f2, expected_version=1),))
    result = port.register(BINDING, batch, "v2")
    assert port.register(BINDING, batch, "v2") == result
    assert port.get(BINDING, RecordKind.FACT, f.fact_id) == f2
    assert port.get(BINDING, RecordKind.FACT, f.fact_id, version=1) == f
    assert port.list(BINDING, RecordKind.FACT) == (f2,)
    with pytest.raises(CoreError):
        port.register(
            BINDING,
            RecordBatch(facts=(FactWrite(replace(f2, version=3), expected_version=1),)),
            "v3",
        )
    with pytest.raises(CoreError):
        port.register(
            BINDING,
            RecordBatch(facts=(FactWrite(replace(f2, version=4), expected_version=2),)),
            "v4",
        )


@pytest.mark.parametrize("invalid", ["sources", "version", "binding", "missing"])
def test_episode_evidence_rechecks_reference_and_source_set(
    port: "MemoryRecordStore", stores: Stores, invalid: str
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    e, e2 = episode(citation(stores)), episode(citation(stores), "e2")
    port.register(BINDING, RecordBatch(episodes=(e, e2)), "v1")
    s = derived(e, e2)
    ev = s.episode_evidence[0]
    if invalid == "sources":
        ev = replace(ev, sources=frozenset((seed(stores),)))
    else:
        ev = replace(
            ev,
            reference=replace(ev.reference, version=99)
            if invalid == "version"
            else replace(ev.reference, binding=OTHER)
            if invalid == "binding"
            else replace(ev.reference, record_id="missing"),
        )
    # Domain prevents mixed Binding; using the other binding also tests the port boundary.
    if invalid == "binding":
        s = replace(
            s,
            binding=OTHER,
            episode_evidence=(
                ev,
                replace(
                    s.episode_evidence[1],
                    reference=replace(s.episode_evidence[1].reference, binding=OTHER),
                ),
            ),
        )
    else:
        s = replace(s, episode_evidence=(ev, s.episode_evidence[1]))
    with pytest.raises(CoreError):
        port.register(s.binding, RecordBatch(semantics=(s,)), "v2")


@pytest.mark.parametrize("action", ["private", "delete", "turn_delete"])
def test_revocation_dependencies_fact_all_versions_and_non_resurrection(
    port: "MemoryRecordStore", stores: Stores, action: str
) -> None:
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch

    e, e2 = episode(citation(stores)), episode(citation(stores), "e2")
    f = fact(e.citations[0])
    s = derived(e, e2)
    link = EpisodeFactLink(
        link_id="l1",
        version=1,
        binding=BINDING,
        created_at=NOW,
        state=RecordState.ACTIVE,
        episode=reference(e),
        fact=reference(f),
    )
    original = RecordBatch(episodes=(e, e2), facts=(FactWrite(f),), links=(link,), semantics=(s,))
    port.register(BINDING, original, "v1")
    f2 = replace(f, version=2, citations=e2.citations, normalized_text="Synthetic revised")
    port.register(BINDING, RecordBatch(facts=(FactWrite(f2, expected_version=1),)), "v2")
    ref = e.citations[0].source.reference
    if action == "delete":
        stores.history.delete(BINDING, ref.conversation_id)
    elif action == "private":
        stores.history.controls(
            BINDING,
            ref.conversation_id,
            ConversationControls(expected_revision=1, private_mode=True),
        )
        stores.history.controls(
            BINDING,
            ref.conversation_id,
            ConversationControls(expected_revision=2, private_mode=False),
        )
    else:
        stores.history.delete_turns(
            BINDING,
            ref.conversation_id,
            TurnDeletionInput(expected_revision=1, turn_revision=1, scope="selected"),
        )
    for r in (e, f, f2, s, link):
        record_ref = reference(r)
        assert (
            port.get(BINDING, record_ref.kind, record_ref.record_id, version=record_ref.version)
            is None
        )
        head = port.head(BINDING, record_ref.kind, record_ref.record_id)
        assert head is not None and head.state == RecordState.SUSPENDED
    assert port.get(BINDING, RecordKind.EPISODE, "e2") == e2
    from .postgres_record_support import events

    event = events(stores.records)[0]
    assert set(port.affected(BINDING, event)) == {reference(r) for r in (e, f, f2, s, link)}
    assert port.affected(OTHER, event) == ()
    with pytest.raises(CoreError):
        port.register(BINDING, original, "v1")
    with pytest.raises(CoreError):
        port.register(
            BINDING,
            RecordBatch(episodes=(replace(e, citations=e2.citations, five_w=e2.five_w),)),
            "new-v",
        )
    with pytest.raises(CoreError):
        port.register(
            BINDING,
            RecordBatch(facts=(FactWrite(replace(f2, version=3), expected_version=2),)),
            "v3",
        )


@pytest.mark.parametrize("kind", list(RecordKind))
def test_suspended_input_is_rejected(
    port: "MemoryRecordStore", stores: Stores, kind: RecordKind
) -> None:
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch

    c = citation(stores)
    e, f = episode(c), fact(c)
    s = replace(derived(e, episode(citation(stores), "e2")), state=RecordState.SUSPENDED)
    link = EpisodeFactLink(
        link_id="l1",
        version=1,
        binding=BINDING,
        created_at=NOW,
        state=RecordState.SUSPENDED,
        episode=reference(e),
        fact=reference(f),
    )
    batch = (
        RecordBatch(episodes=(replace(e, state=RecordState.SUSPENDED),))
        if kind is RecordKind.EPISODE
        else RecordBatch(facts=(FactWrite(replace(f, state=RecordState.SUSPENDED)),))
        if kind is RecordKind.FACT
        else RecordBatch(semantics=(s,))
        if kind is RecordKind.SEMANTIC
        else RecordBatch(links=(link,))
    )
    with pytest.raises(CoreError):
        port.register(BINDING, batch, "v1")


def test_direct_semantic_roundtrip_and_revocation(
    port: "MemoryRecordStore", stores: Stores
) -> None:
    from digital_souls_core.memory_record_store import RecordBatch

    c = citation(stores)
    s = Semantic(
        semantic_id="direct",
        version=1,
        binding=BINDING,
        normalized_text="Synthetic direct statement",
        created_at=NOW,
        last_user_mentioned_at=NOW,
        state=RecordState.ACTIVE,
        formation_type=FormationType.DIRECT_EXTRACTION,
        proposition=Proposition(subject="synthetic", attribute="likes", value="tea"),
        applicability=TemporalValue(),
        citations=(c,),
        episode_evidence=(),
    )
    port.register(BINDING, RecordBatch(semantics=(s,)), "direct-v1")
    assert port.get(BINDING, RecordKind.SEMANTIC, "direct") == s
    stores.history.delete(BINDING, c.source.reference.conversation_id)
    assert port.list(BINDING, RecordKind.SEMANTIC) == ()
    with pytest.raises(CoreError):
        port.register(BINDING, RecordBatch(semantics=(s,)), "direct-v1")


def test_links_reject_foreign_or_missing_targets(port: "MemoryRecordStore", stores: Stores) -> None:
    from digital_souls_core.memory_record_store import FactWrite, RecordBatch

    c = citation(stores)
    e, f = episode(c), fact(c)
    port.register(BINDING, RecordBatch(episodes=(e,), facts=(FactWrite(f),)), "v1")
    for binding in (BINDING, OTHER):
        link = EpisodeFactLink(
            link_id="l1",
            version=1,
            binding=binding,
            created_at=NOW,
            state=RecordState.ACTIVE,
            episode=replace(reference(e), binding=binding),
            fact=replace(reference(f), binding=binding, version=99 if binding == BINDING else 1),
        )
        with pytest.raises(CoreError):
            port.register(binding, RecordBatch(links=(link,)), "v2")
    assert port.list(BINDING, RecordKind.EPISODE_FACT_LINK) == ()


def test_concurrent_retries_and_fact_cas(port: "MemoryRecordStore", stores: Stores) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from digital_souls_core.memory_record_store import FactWrite, RecordBatch

    e = episode(citation(stores))
    barrier = Barrier(2)

    def retry() -> tuple[RecordRef, ...]:
        barrier.wait(timeout=5)
        return port.register(BINDING, RecordBatch(episodes=(e,)), "v1")

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: retry(), range(2)))
    assert results[0] == results[1] and port.list(BINDING, RecordKind.EPISODE) == (e,)
    f = fact(citation(stores))
    port.register(BINDING, RecordBatch(facts=(FactWrite(f),)), "fact-v1")
    barrier = Barrier(2)
    # Competing revisions use distinct new source sets, so this tests CAS rather
    # than the identical-key retry shortcut.
    changes = (
        replace(f, version=2, citations=(citation(stores),), normalized_text="first"),
        replace(f, version=2, citations=(citation(stores),), normalized_text="second"),
    )

    def update(change: Fact) -> bool:
        barrier.wait(timeout=5)
        try:
            port.register(
                BINDING, RecordBatch(facts=(FactWrite(change, expected_version=1),)), "fact-v2"
            )
            return True
        except CoreError as error:
            assert error.code == "memory_version_conflict"
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(update, changes)) == [False, True]
    assert port.get(BINDING, RecordKind.FACT, "f1", version=1) == f
