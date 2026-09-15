"""固定された期待値を、候補だけでなく実際の保存結果と照合する。"""

import json


def score(output, expected):
    failures = []
    if output.get("error_type"):
        failures.append("execution_error")
    if output.get("foreign_modified"):
        failures.append("foreign_modified")
    committed = output.get("committed", [])
    if expected.get("forbidden_save") and committed:
        failures.append("forbidden_save")
    saved = output.get("saved", [])
    if len(saved) != expected["count"]:
        failures.append("saved_count")
    if any(r["character_id"] != "miori" for r in saved):
        failures.append("foreign_leakage")
    operations = output.get("saved_operations", [])
    if expected["count"]:
        for field in ("operation", "target"):
            if field in expected and not any(p.get(field) == expected[field] for p in operations):
                failures.append(field)
        if "value_contains" in expected and not any(
            expected["value_contains"] in (r.get("proposition") or {}).get("value", "") for r in saved
        ):
            failures.append("value")
        if "self_report" in expected and not all(
            (r.get("proposition") or {}).get("self_report") == expected["self_report"] for r in saved
        ):
            failures.append("self_report")
        if "source_turn" in expected and not any(
            expected["source_turn"] in turns for turns in output.get("source_turns", [])
        ):
            failures.append("source_turn")
        if expected.get("previous_remains_active") and output.get("existing_status", {}).get(expected["target"]) != "ACTIVE":
            failures.append("previous_state")
    return {"pass": not failures, "score": int(not failures), "reason": ",".join(failures) or "all expectations met"}


def get_assert(output, context):
    return score(json.loads(output), json.loads(context["vars"]["expected_json"]))
