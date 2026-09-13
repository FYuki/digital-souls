"""LLMを採点者に使わず、行為者の集合を固定正解と照合する。"""

import json


def actor_match(output, expected):
    if not isinstance(output, dict) or output.get("error_type"):
        return False
    who = output.get("who")
    if not isinstance(who, list):
        return False
    if any(not isinstance(person, dict) for person in who):
        return False
    actors = [person for person in who if person.get("role") == "ACTOR"]
    if len(actors) != len(expected):
        return False
    # 同一人物の重複や余分な行為者も不正解。未知は行為者を補わないことを正解とする。
    remaining = list(expected)
    for actor in actors:
        matches = [i for i, truth in enumerate(remaining)
                   if actor.get("entity_id") == truth["entity_id"]
                   and actor.get("name") in truth["names"]]
        if len(matches) != 1:
            return False
        remaining.pop(matches[0])
    return not remaining


def get_assert(output, context):
    try:
        passed = actor_match(json.loads(output), json.loads(context["vars"]["expected_json"]))
    except (ValueError, TypeError, KeyError):
        passed = False
    return {"pass": passed, "score": int(passed),
            "reason": "actor_set_match" if passed else "actor_set_mismatch_or_error"}
