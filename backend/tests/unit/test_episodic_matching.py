"""保存可能性と、別Factの同一性確認を分ける。"""

from datetime import UTC, datetime

import pytest

from app.memory.episodic.contracts import (
    FiveW, NarrativeContext, Person, PersonRole, Place, RecordKind,
    ResolvedTime, TimeParts, What,
)
from app.memory.episodic.matching import same_five_w
from app.memory.episodic.rendering import render_five_w


def complete() -> FiveW:
    return FiveW(
        who=(Person(name="利用者", role=PersonRole.ACTOR, entity_id="person:user"),),
        what=What(predicate="食べた", object="うどん", polarity="AFFIRMED", actuality="OCCURRED"),
        when=ResolvedTime(parts=TimeParts(year=2026, month=9, day=12),
                          timezone="Asia/Tokyo", reference_at=datetime(2026, 9, 13, tzinfo=UTC)),
        where=Place(name="駅前のうどん店", entity_id="place:udon"),
        why="昼食のため", context=NarrativeContext.REPORTED,
    )


def test_known_five_w_match_ignores_when_the_claim_was_heard():
    left = complete()
    right = left.model_copy(update={"when": left.when.model_copy(
        update={"reference_at": datetime(2026, 9, 14, tzinfo=UTC)})})
    assert same_five_w(left, right)
    # この結果は必要条件のみ。同日別回の可能性は登録サービスで出典から確認する。


@pytest.mark.parametrize("field,value", [
    ("who", ()),
    ("who", (Person(name="利用者", role=PersonRole.ACTOR),)),
    ("who", (Person(name="利用者", role=PersonRole.ACTOR, entity_id="person:other"),)),
    ("who", (Person(name="利用者", role=PersonRole.TOPIC, entity_id="person:user"),)),
    ("what", What(predicate="食べた", object="うどん", polarity="NEGATED", actuality="OCCURRED")),
    ("what", What(predicate="食べた", object="うどん", polarity="AFFIRMED", actuality="PLANNED")),
    ("what", What(predicate="食べた", polarity="AFFIRMED", actuality="OCCURRED")),
    ("when", None),
    ("when", ResolvedTime(parts=TimeParts(year=2026, month=9),
                          timezone="Asia/Tokyo", reference_at=datetime(2026, 10, 13, tzinfo=UTC))),
    ("where", Place(name="駅前のうどん店")),
    ("why", None),
    ("context", NarrativeContext.HYPOTHETICAL),
    ("context", NarrativeContext.FICTIONAL),
    ("context", NarrativeContext.UNKNOWN),
])
def test_missing_or_different_five_w_never_proves_same_fact(field, value):
    original = complete()
    modified = original.model_copy(update={field: value})
    assert not same_five_w(original, modified)
    assert not same_five_w(modified, original)


def test_equal_unknowns_are_not_evidence():
    assert not same_five_w(FiveW(what=What(predicate="話した")),
                           FiveW(what=What(predicate="話した")))
    original = complete()
    for field, value in [
        ("where", Place(name="同名の店")), ("why", None),
        ("what", What(predicate="食べた", object="うどん")),
    ]:
        incomplete = original.model_copy(update={field: value})
        assert not same_five_w(incomplete, incomplete)


@pytest.mark.parametrize("context,label", [
    (NarrativeContext.HYPOTHETICAL, "仮定の話"),
    (NarrativeContext.FICTIONAL, "創作の話"),
    (NarrativeContext.UNKNOWN, "実際の出来事か不明"),
    (NarrativeContext.REPORTED, "申告・観測された内容"),
])
def test_context_and_roles_survive_search_text_projection(context, label):
    value = complete().model_copy(update={"context": context})
    output = render_five_w(value, kind=RecordKind.FACT)
    assert label in output
    assert "取得した情報" in output
    assert "行為者:利用者" in output
    assert "2026年9月12日" in output
    assert "昼食のため" in output


def test_predicate_only_projection_does_not_invent_an_actor_or_time():
    output = render_five_w(FiveW(what=What(predicate="聞いた")), kind=RecordKind.EPISODE)
    assert "人物:不明" in output
    assert "日時:不明" in output
    assert "場所:不明" in output
    assert "理由:不明" in output
    assert "実際の出来事か不明" in output
