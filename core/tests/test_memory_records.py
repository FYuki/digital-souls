"""Canonical record contracts: synthetic evidence, no storage or inference."""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from digital_souls_core.character import AccessScope
from digital_souls_core.history import Binding, SourceReference
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
    PartialDateTime,
    Proposition,
    RecordHead,
    RecordKind,
    RecordRef,
    RecordState,
    Semantic,
    Speaker,
    TemporalValue,
    TimePrecision,
    dependency_invalidated,
)

pytestmark = pytest.mark.ut
NOW = datetime(2026, 10, 7, 9, tzinfo=UTC)
BINDING = Binding(AccessScope("subject", "client", "local-private"), "character")
OTHER = replace(BINDING, character_id="other")


def citation(index: int = 0, *, speaker: Speaker = Speaker.USER) -> Citation:
    return Citation(
        binding=BINDING,
        source=SourceVersion(SourceReference("conversation", 1, index), 0),
        speaker=speaker,
        start=0,
        end=5,
    )


def episode() -> Episode:
    return Episode(
        episode_id="episode-1",
        version=1,
        binding=BINDING,
        normalized_text="合成の保存文",
        created_at=NOW,
        last_user_mentioned_at=None,
        state=RecordState.ACTIVE,
        five_w=FiveW(predicate="聞いた"),
        experience_time=TemporalValue(),
        experienced_at=None,
        context=EpisodeContext.ACTUAL,
        citations=(citation(),),
    )


def fact() -> Fact:
    return Fact(
        fact_id="caller-assigned-fact",
        version=1,
        binding=BINDING,
        normalized_text="合成の申告内容",
        created_at=NOW,
        last_user_mentioned_at=None,
        state=RecordState.ACTIVE,
        five_w=FiveW(predicate="食べた", object="そば"),
        target_time=TemporalValue(),
        citations=(citation(),),
    )


def ref(kind: RecordKind, record_id: str = "record", version: int = 1) -> RecordRef:
    return RecordRef(kind=kind, record_id=record_id, version=version, binding=BINDING)


def evidence(record_id: str, *indices: int) -> EpisodeEvidence:
    return EpisodeEvidence(
        reference=ref(RecordKind.EPISODE, record_id),
        sources=frozenset(citation(i).source.reference for i in indices),
    )


def semantic() -> Semantic:
    return Semantic(
        semantic_id="semantic-1",
        version=1,
        binding=BINDING,
        normalized_text="合成の命題",
        created_at=NOW,
        last_user_mentioned_at=NOW,
        state=RecordState.ACTIVE,
        formation_type=FormationType.DIRECT_EXTRACTION,
        proposition=Proposition(subject="主体", attribute="属性", value="値"),
        applicability=TemporalValue(),
        citations=(citation(),),
        episode_evidence=(),
    )


def link() -> EpisodeFactLink:
    return EpisodeFactLink(
        link_id="link-1",
        version=1,
        binding=BINDING,
        created_at=NOW,
        state=RecordState.ACTIVE,
        episode=ref(RecordKind.EPISODE, "episode-1"),
        fact=ref(RecordKind.FACT, "caller-assigned-fact"),
    )


def test_predicate_only_episode_keeps_unknowns() -> None:
    value = episode()
    assert value.five_w == FiveW(predicate="聞いた")
    assert value.five_w.object is None
    assert value.five_w.who is None
    assert value.five_w.when is None
    assert value.five_w.where is None
    assert value.five_w.why is None
    assert value.experience_time == TemporalValue()
    assert value.experienced_at is None
    assert value.last_user_mentioned_at is None
    assert fact().target_time == semantic().applicability == TemporalValue()


@pytest.mark.parametrize("context", list(EpisodeContext))
def test_episode_preserves_context_and_experienced_time(context: EpisodeContext) -> None:
    value = replace(episode(), context=context, experienced_at=NOW)
    assert value.context is context
    assert value.experienced_at == NOW
    assert value.five_w.who is None


def test_multiple_citations_keep_individual_source_versions_and_ranges() -> None:
    second = replace(
        citation(1), source=SourceVersion(SourceReference("another", 2, 3), 7), start=2, end=8
    )
    value = replace(episode(), citations=(citation(), second))
    assert value.citations[0].source.epoch == 0
    assert value.citations[1].source.epoch == 7
    assert value.citations[1].source.reference.turn_revision == 2
    assert (value.citations[1].start, value.citations[1].end) == (2, 8)


@pytest.mark.parametrize(
    "changes",
    [
        {"start": -1},
        {"end": 0},
        {"start": 5},
        {"end": -1},
        {"start": True},
        {"end": 1.5},
        {"speaker": "unknown"},
        {"source": SourceVersion(SourceReference("conversation", 1, 0), -1)},
        {"source": SourceVersion(SourceReference("conversation", 0, 0), 0)},
        {"source": SourceVersion(SourceReference("conversation", 1, -1), 0)},
        {"source": SourceVersion(SourceReference(" ", 1, 0), 0)},
        {"source": SourceVersion(SourceReference("conversation", 1, 0), True)},
        {"binding": None},
        {"source": None},
    ],
)
def test_citation_rejects_invalid_address_range_or_role(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(citation(), **changes)


@pytest.mark.parametrize("speaker", list(Speaker))
def test_citation_accepts_explicit_speaker(speaker: Speaker) -> None:
    assert citation(speaker=speaker).speaker is speaker


@pytest.mark.parametrize("field", ["predicate", "object", "who", "where"])
@pytest.mark.parametrize("value", ["", " \t", 42])
def test_five_w_rejects_empty_or_invalid_known_content(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        replace(FiveW(predicate="聞いた"), **{field: value})


def test_five_w_preserves_known_values_and_explicit_reason() -> None:
    why = ExplicitReason(text="明示された合成の理由", citations=(citation(),))
    when = TemporalValue(start=PartialDateTime(precision=TimePrecision.YEAR, year=2026))
    value = FiveW(
        predicate="行った", object="展示", who="合成人物", when=when, where="架空会場", why=why
    )
    assert value.when is when and value.why is why
    assert replace(episode(), five_w=value).five_w == value
    assert replace(fact(), five_w=value).five_w == value


@pytest.mark.parametrize("changes", [{"text": " "}, {"citations": ()}, {"citations": []}])
def test_reason_requires_text_and_immutable_citations(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(ExplicitReason(text="明示理由", citations=(citation(),)), **changes)


@pytest.mark.parametrize("factory", [episode, fact])
def test_reason_cannot_cross_binding(factory: Any) -> None:
    reason = ExplicitReason(text="明示理由", citations=(replace(citation(), binding=OTHER),))
    with pytest.raises(ValueError):
        replace(factory(), five_w=FiveW(predicate="聞いた", why=reason))


@pytest.mark.parametrize(
    "field,value", [("predicate", None), ("when", "unknown"), ("why", "推測した理由")]
)
def test_five_w_rejects_untyped_reason_or_time(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        replace(FiveW(predicate="聞いた"), **{field: value})


@pytest.mark.parametrize(
    "precision,parts",
    [
        (TimePrecision.YEAR, {}),
        (TimePrecision.MONTH, {"month": 2}),
        (TimePrecision.DAY, {"month": 2, "day": 29}),
        (TimePrecision.HOUR, {"month": 2, "day": 29, "hour": 12}),
        (TimePrecision.MINUTE, {"month": 2, "day": 29, "hour": 12, "minute": 30}),
        (TimePrecision.SECOND, {"month": 2, "day": 29, "hour": 12, "minute": 30, "second": 45}),
    ],
)
def test_partial_time_retains_exact_precision(
    precision: TimePrecision, parts: dict[str, int]
) -> None:
    point = PartialDateTime(precision=precision, year=2024, **parts)
    value = TemporalValue(start=point, timezone="Asia/Tokyo")
    assert value.start is point and value.end is None
    assert value.timezone == "Asia/Tokyo"
    assert point.precision is precision
    if precision is TimePrecision.MONTH:
        assert point.day is None


@pytest.mark.parametrize(
    "changes",
    [
        {"precision": "month"},
        {"year": 0},
        {"year": 10000},
        {"year": True},
        {"month": 13},
        {"month": 0},
        {"month": True},
        {"day": 30},
        {"year": 2025},
        {"hour": 24},
        {"hour": -1},
        {"minute": 60},
        {"second": 60},
        {"minute": 1.5},
        {"month": None},
        {"precision": TimePrecision.MONTH},
        {"precision": TimePrecision.YEAR},
        {"day": None},
        {"hour": None},
        {"minute": None},
        {"second": None},
    ],
)
def test_partial_time_rejects_invalid_calendar_or_precision(changes: dict[str, Any]) -> None:
    point = PartialDateTime(
        precision=TimePrecision.SECOND, year=2024, month=2, day=29, hour=12, minute=30, second=45
    )
    with pytest.raises(ValueError):
        replace(point, **changes)


def test_time_range_accepts_equality_and_order_without_completing_partial_dates() -> None:
    start = PartialDateTime(precision=TimePrecision.MONTH, year=2026, month=9)
    end = replace(start, month=10)
    assert TemporalValue(start=start, end=end).end == end
    assert TemporalValue(start=start, end=start).start == start
    value = TemporalValue(start=start, end=end)
    assert value.start is not None and value.start.day is None
    assert TemporalValue(timezone="Asia/Tokyo").start is None


@pytest.mark.parametrize(
    "changes",
    [
        {"end": PartialDateTime(precision=TimePrecision.YEAR, year=2025)},
        {"end": PartialDateTime(precision=TimePrecision.MONTH, year=2026, month=1)},
        {"start": None, "end": PartialDateTime(precision=TimePrecision.YEAR, year=2026)},
        {"start": "unknown"},
        {"end": "unknown"},
        {"timezone": " "},
        {"timezone": 9},
    ],
)
def test_time_range_rejects_reversed_uncomparable_or_untyped_values(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        replace(
            TemporalValue(start=PartialDateTime(precision=TimePrecision.YEAR, year=2026)), **changes
        )


@pytest.mark.parametrize(
    "factory,id_field",
    [(episode, "episode_id"), (fact, "fact_id"), (semantic, "semantic_id"), (link, "link_id")],
)
@pytest.mark.parametrize(
    "changes",
    [
        {"version": 0},
        {"version": -1},
        {"version": True},
        {"version": 1.5},
        {"created_at": NOW.replace(tzinfo=None)},
        {"created_at": None},
        {"state": "ACTIVE"},
        {"binding": None},
    ],
)
def test_common_record_invariants(factory: Any, id_field: str, changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(factory(), **changes)
    with pytest.raises(ValueError):
        replace(factory(), **{id_field: " "})


@pytest.mark.parametrize("factory", [episode, fact, semantic, link])
def test_common_record_accepts_versions_states_and_aware_timestamps(factory: Any) -> None:
    local = NOW.astimezone(timezone(timedelta(hours=9)))
    value = replace(
        factory(),
        version=2,
        created_at=local,
        state=RecordState.SUSPENDED,
    )
    assert value.version == 2 and value.state is RecordState.SUSPENDED
    assert value.created_at is local
    with pytest.raises(FrozenInstanceError):
        value.version = 3


@pytest.mark.parametrize("factory", [episode, fact, semantic])
@pytest.mark.parametrize(
    "changes",
    [
        {"normalized_text": ""},
        {"normalized_text": " \n"},
        {"normalized_text": None},
        {"last_user_mentioned_at": NOW.replace(tzinfo=None)},
    ],
)
def test_content_records_reject_invalid_text_or_last_mention(
    factory: Any, changes: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        replace(factory(), **changes)


@pytest.mark.parametrize("factory", [episode, fact, semantic])
def test_content_records_accept_aware_last_mention(factory: Any) -> None:
    local = NOW.astimezone(timezone(timedelta(hours=9)))
    assert replace(factory(), last_user_mentioned_at=local).last_user_mentioned_at is local


@pytest.mark.parametrize("factory", [episode, fact, semantic])
def test_records_reject_foreign_binding_citations(factory: Any) -> None:
    with pytest.raises(ValueError):
        replace(factory(), citations=(replace(citation(), binding=OTHER),))


@pytest.mark.parametrize("factory", [episode, fact])
@pytest.mark.parametrize("changes", [{"citations": ()}, {"citations": []}, {"five_w": None}])
def test_episode_fact_require_typed_content_and_citations(
    factory: Any, changes: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        replace(factory(), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"experienced_at": NOW.replace(tzinfo=None)},
        {"context": "ACTUAL"},
        {"experience_time": None},
    ],
)
def test_episode_rejects_invalid_experience_metadata(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(episode(), **changes)


def test_fact_id_is_independent_of_five_w_and_content_versions() -> None:
    initial = fact()
    corrected = replace(
        initial,
        version=2,
        five_w=FiveW(predicate="食べた", object="うどん"),
        target_time=TemporalValue(start=PartialDateTime(precision=TimePrecision.YEAR, year=2025)),
        normalized_text="別の版の保存文",
        citations=(citation(1),),
    )
    separate = replace(initial, fact_id="another-caller-assigned-id")
    assert initial.fact_id == corrected.fact_id
    assert initial.five_w != corrected.five_w
    assert initial.citations != corrected.citations
    assert initial.version == 1 and corrected.version == 2
    assert initial.five_w == separate.five_w and initial.fact_id != separate.fact_id
    with pytest.raises(ValueError):
        replace(initial, target_time=None)  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", list(RecordKind))
def test_typed_record_ref_preserves_identity_and_version(kind: RecordKind) -> None:
    value = ref(kind, version=2)
    assert (value.kind, value.record_id, value.version, value.binding) == (
        kind,
        "record",
        2,
        BINDING,
    )


@pytest.mark.parametrize(
    "changes",
    [{"kind": "episode"}, {"record_id": " "}, {"version": 0}, {"version": True}, {"binding": None}],
)
def test_record_ref_rejects_invalid_identity(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(ref(RecordKind.EPISODE), **changes)


def test_episode_fact_link_has_only_content_free_reference_fields() -> None:
    value = link()
    assert {item.name for item in fields(value)} == {
        "link_id",
        "version",
        "binding",
        "created_at",
        "state",
        "episode",
        "fact",
    }
    assert not hasattr(value, "normalized_text")
    assert not hasattr(value, "last_user_mentioned_at")


@pytest.mark.parametrize("field", ["normalized_text", "last_user_mentioned_at"])
def test_episode_fact_link_rejects_content_fields(field: str) -> None:
    changes: dict[str, Any] = {field: None}
    with pytest.raises(TypeError):
        replace(link(), **changes)


def test_episode_fact_link_keeps_both_typed_versioned_references() -> None:
    value = replace(
        link(), episode=ref(RecordKind.EPISODE, "ep", 2), fact=ref(RecordKind.FACT, "f", 3)
    )
    assert value.episode.version == 2 and value.fact.version == 3


@pytest.mark.parametrize(
    "field,reference",
    [
        ("episode", ref(RecordKind.FACT)),
        ("fact", ref(RecordKind.EPISODE)),
        ("episode", replace(ref(RecordKind.EPISODE), binding=OTHER)),
        ("fact", replace(ref(RecordKind.FACT), binding=OTHER)),
        ("episode", None),
        ("fact", None),
    ],
)
def test_link_rejects_wrong_kind_or_binding(field: str, reference: Any) -> None:
    with pytest.raises(ValueError):
        replace(link(), **{field: reference})


@pytest.mark.parametrize("field", ["subject", "attribute", "value"])
@pytest.mark.parametrize("value", ["", " \t", None])
def test_proposition_requires_three_nonempty_values(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        replace(semantic().proposition, **{field: value})


def test_direct_extraction_accepts_user_evidence_with_other_roles() -> None:
    value = replace(semantic(), citations=(citation(speaker=Speaker.ASSISTANT), citation(1)))
    assert len(value.citations) == 2


@pytest.mark.parametrize(
    "citations",
    [
        (),
        (citation(speaker=Speaker.ASSISTANT),),
        (citation(speaker=Speaker.TOOL), citation(1, speaker=Speaker.SYSTEM)),
    ],
)
def test_direct_extraction_rejects_missing_user_evidence(citations: tuple[Citation, ...]) -> None:
    with pytest.raises(ValueError):
        replace(semantic(), citations=citations)


@pytest.mark.parametrize(
    "changes",
    [
        {"formation_type": "DIRECT_EXTRACTION"},
        {"proposition": None},
        {"applicability": None},
        {"episode_evidence": []},
        {"citations": []},
    ],
)
def test_semantic_rejects_untyped_metadata(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(semantic(), **changes)


def test_experience_derived_accepts_two_disjoint_episode_sources() -> None:
    value = replace(
        semantic(),
        formation_type=FormationType.EXPERIENCE_DERIVED,
        citations=(),
        episode_evidence=(evidence("ep-1", 0, 1), evidence("ep-2", 2)),
    )
    assert value.formation_type is FormationType.EXPERIENCE_DERIVED
    assert value.citations == ()


@pytest.mark.parametrize(
    "episodes",
    [
        (),
        (evidence("ep-1", 0),),
        (evidence("ep-1", 0), evidence("ep-1", 1)),
        (evidence("ep-1", 0), evidence("ep-2", 0)),
        (evidence("ep-1", 0, 1), evidence("ep-2", 1, 2)),
        # Duplicate IDs must union their sources, not choose the convenient first entry.
        (evidence("ep-1", 0), evidence("ep-1", 1), evidence("ep-2", 1)),
        (evidence("ep-1", 0, 1), evidence("ep-2", 1, 2), evidence("ep-3", 0, 2)),
    ],
)
def test_experience_derived_rejects_insufficient_independence(
    episodes: tuple[EpisodeEvidence, ...],
) -> None:
    with pytest.raises(ValueError):
        replace(
            semantic(), formation_type=FormationType.EXPERIENCE_DERIVED, episode_evidence=episodes
        )


def test_independence_counts_source_address_not_epoch_or_episode_version() -> None:
    first = evidence("ep-1", 0)
    second = EpisodeEvidence(
        reference=ref(RecordKind.EPISODE, "ep-2", 2),
        sources=frozenset(
            (
                replace(
                    citation(), source=SourceVersion(citation().source.reference, 9)
                ).source.reference,
            )
        ),
    )
    with pytest.raises(ValueError):
        replace(
            semantic(),
            formation_type=FormationType.EXPERIENCE_DERIVED,
            episode_evidence=(first, second),
        )


def test_independence_can_find_disjoint_pair_after_overlapping_evidence() -> None:
    value = replace(
        semantic(),
        formation_type=FormationType.EXPERIENCE_DERIVED,
        episode_evidence=(evidence("ep-1", 0, 1), evidence("ep-2", 1), evidence("ep-3", 2)),
    )
    assert len(value.episode_evidence) == 3


@pytest.mark.parametrize(
    "changes",
    [
        {"reference": ref(RecordKind.FACT)},
        {"sources": frozenset()},
        {"sources": set()},
        {"sources": frozenset(("unknown",))},
    ],
)
def test_episode_evidence_requires_typed_episode_and_nonempty_source_set(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        replace(evidence("ep", 0), **changes)


@pytest.mark.parametrize("formation_type", list(FormationType))
def test_semantic_rejects_foreign_episode_binding(formation_type: FormationType) -> None:
    foreign = replace(
        evidence("ep-2", 1), reference=replace(ref(RecordKind.EPISODE), binding=OTHER)
    )
    with pytest.raises(ValueError):
        replace(
            semantic(),
            formation_type=formation_type,
            episode_evidence=(evidence("ep-1", 0), foreign),
        )


@pytest.mark.parametrize(
    "factory,kind,id_field",
    [
        (episode, RecordKind.EPISODE, "episode_id"),
        (fact, RecordKind.FACT, "fact_id"),
        (semantic, RecordKind.SEMANTIC, "semantic_id"),
        (link, RecordKind.EPISODE_FACT_LINK, "link_id"),
    ],
)
def test_dependency_invalidates_missing_updated_suspended_or_foreign_record(
    factory: Any,
    kind: RecordKind,
    id_field: str,
) -> None:
    current = factory()
    dependency = ref(kind, getattr(current, id_field))
    assert not dependency_invalidated(dependency, current)
    assert dependency_invalidated(dependency, None)
    assert dependency_invalidated(dependency, replace(current, version=2))
    assert dependency_invalidated(replace(dependency, version=2), current)
    assert dependency_invalidated(dependency, replace(current, state=RecordState.SUSPENDED))
    assert dependency_invalidated(replace(dependency, binding=OTHER), current)
    assert dependency_invalidated(replace(dependency, record_id="different"), current)
    other_kind = RecordKind.FACT if kind is RecordKind.EPISODE else RecordKind.EPISODE
    assert dependency_invalidated(replace(dependency, kind=other_kind), current)


@pytest.mark.parametrize("factory", [episode, fact, semantic])
def test_repr_omits_normalized_and_structured_content(factory: Any) -> None:
    value = factory()
    assert value.normalized_text not in repr(value)
    assert "聞いた" not in repr(value) and "そば" not in repr(value)
    assert "主体" not in repr(value) and "属性" not in repr(value) and "値" not in repr(value)
    reason = ExplicitReason(text="明示された非公開合成理由", citations=(citation(),))
    assert reason.text not in repr(reason)
    assert "秘密の述語" not in repr(FiveW(predicate="秘密の述語", why=reason))
    assert "秘密の値" not in repr(Proposition(subject="s", attribute="a", value="秘密の値"))


@pytest.mark.parametrize("state", list(RecordState))
def test_dependency_can_check_content_free_head_after_erasure(state: RecordState) -> None:
    dependency = ref(RecordKind.FACT, "fact-1", 2)
    current = RecordHead(reference=dependency, state=state)
    assert dependency_invalidated(dependency, current) is (state is RecordState.SUSPENDED)
    assert dependency_invalidated(
        dependency, replace(current, reference=replace(dependency, version=3))
    )
    assert dependency_invalidated(
        dependency, replace(current, reference=replace(dependency, binding=OTHER))
    )


@pytest.mark.parametrize("changes", [{"reference": None}, {"state": "ACTIVE"}])
def test_content_free_head_rejects_untyped_values(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(RecordHead(reference=ref(RecordKind.FACT), state=RecordState.ACTIVE), **changes)
