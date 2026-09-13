"""簡素化schemaが不正な日時・人物対応を拒否し、有効な精度を保持する。"""
import pytest
import jsonschema
from app.memory.episodic.extraction_contracts import GroundedContent
from app.memory.formation.episodic_extractor import generation_schema
from evals.episodic_quality.ground_schema import constrained_ground_schema

def fixtures():
    schema = constrained_ground_schema(generation_schema(GroundedContent), {
        "candidate": {"kind": "FACT"},
        "fragments": [{"speaker": {"entity_id": "speaker:user", "name": "ユーザー"},
                       "addressee": {"entity_id": "character:miori", "name": "光織"}}],
    })
    value = {"five_w": {"who": [], "what": {"predicate": "歩いた", "object": None,
                       "polarity": "AFFIRMED", "actuality": "OCCURRED"},
                       "when": None, "where": None, "why": None, "context": "REPORTED"},
             "time_source": None}
    return schema, value

def time_value():
    return {"parts": dict.fromkeys(["year","month","day","hour","minute","second"]),
            "end": None, "range_kind": "POINT", "relative_unit": "DAY", "relative_offset": -1}

@pytest.mark.parametrize("kind", ["relative", "partial", "range"])
def test_valid_time_forms_preserve_precision(kind):
    schema, value = fixtures()
    temporal = time_value()
    if kind != "relative":
        temporal.update(relative_unit=None, relative_offset=None)
        temporal["parts"]["month"] = 9
    if kind == "range":
        temporal.update(end=temporal["parts"].copy(), range_kind="UNCERTAINTY")
    value["five_w"]["when"] = temporal
    jsonschema.validate(value, schema)

@pytest.mark.parametrize("kind", ["mixed", "missing_offset", "missing_end", "wrong_person"])
def test_inconsistent_forms_are_not_generatable(kind):
    schema, value = fixtures()
    temporal = time_value()
    if kind == "mixed":
        temporal["parts"]["year"] = 2026
    elif kind == "missing_offset":
        temporal["relative_offset"] = None
    elif kind == "missing_end":
        temporal["range_kind"] = "DURATION"
    else:
        value["five_w"]["who"] = [{"name": "澪", "role": "ACTOR", "entity_id": "character:miori"}]
    value["five_w"]["when"] = temporal
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(value, schema)
