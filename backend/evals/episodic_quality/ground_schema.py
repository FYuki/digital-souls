"""5Wの意味を変えず、同時に成立しない生成フィールドの組を除く比較用schema。"""
from copy import deepcopy

def constrained_ground_schema(schema, payload):
    result = deepcopy(schema)
    definitions = result.get("$defs", {})
    temporal = definitions.get("TimeExpression")
    if temporal is not None:
        base = temporal["properties"]
        def branch(properties):
            return {"type": "object", "properties": properties,
                    "required": list(properties), "additionalProperties": False}
        null = {"type": "null"}
        parts = definitions["TimeParts"]["properties"]
        empty_parts = {"type": "object", "properties": {k: null for k in parts},
                       "required": list(parts), "additionalProperties": False}
        relative = deepcopy(base)
        relative.update({"parts": empty_parts, "end": null, "range_kind": {"const": "POINT"},
                         "relative_unit": {"enum": ["DAY", "MONTH", "YEAR"]},
                         "relative_offset": {"type": "integer", "minimum": -12000, "maximum": 12000}})
        absolute = deepcopy(base)
        absolute.update({"end": null, "range_kind": {"const": "POINT"},
                         "relative_unit": null, "relative_offset": null})
        interval = deepcopy(absolute)
        interval.update({"end": {"$ref": "#/$defs/TimeParts"},
                         "range_kind": {"enum": ["UNCERTAINTY", "DURATION"]}})
        definitions["TimeExpression"] = {
            "title": "TimeExpression",
            "anyOf": [branch(relative), branch(absolute), branch(interval)],
        }
    # FactのWhoの役割は対象行為のACTOR。会話参加者のSPEAKER/LISTENERとは分離する。
    if payload.get("candidate", {}).get("kind") == "FACT" and "Person" in definitions:
        person = definitions["Person"]
        base = deepcopy(person["properties"])
        base["role"] = {"const": "ACTOR"}
        pairs = {}
        for fragment in payload.get("fragments", []):
            for role in ("speaker", "addressee"):
                identity = fragment.get(role, {})
                if identity.get("name") is not None and identity.get("entity_id") is not None:
                    pairs[identity["entity_id"]] = identity["name"]
        for record in payload.get("known_records", []):
            for individual in (record.get("five_w") or {}).get("who", []):
                if individual.get("entity_id") is not None:
                    pairs[individual["entity_id"]] = individual["name"]
        choices = []
        for identity, name in pairs.items():
            properties = deepcopy(base)
            properties.update({"name": {"const": name}, "entity_id": {"const": identity}})
            choices.append({"type": "object", "properties": properties,
                            "required": list(properties), "additionalProperties": False})
        other = deepcopy(base)
        other["entity_id"] = {"type": "null"}
        choices.append({"type": "object", "properties": other,
                        "required": list(other), "additionalProperties": False})
        definitions["Person"] = {"title": "Person", "anyOf": choices}
    return result
