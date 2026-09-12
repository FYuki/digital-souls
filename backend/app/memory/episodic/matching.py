"""5WによるFact統合と、対象が明確な補足・訂正を分離する。"""

from app.memory.episodic.contracts import FiveW, NarrativeContext


def same_five_w(left: FiveW, right: FiveW) -> bool:
    """必要条件だけを検証する。同一の出来事への再言及の根拠は別途必要。"""
    if left.context is NarrativeContext.UNKNOWN or left.context != right.context:
        return False
    if not left.who or not right.who:
        return False
    if any(person.entity_id is None for person in (*left.who, *right.who)):
        return False
    if {(p.entity_id, p.role) for p in left.who} != {
        (p.entity_id, p.role) for p in right.who
    }:
        return False
    if left.what.polarity == "UNKNOWN" or left.what.actuality == "UNKNOWN":
        return False
    if left.what.object is None or left.what != right.what:
        return False
    if left.when is None or right.when is None:
        return False
    if left.when.parts.precision in {"UNKNOWN", "PARTIAL"}:
        return False
    if left.when.end is not None and left.when.end.precision in {"UNKNOWN", "PARTIAL"}:
        return False
    # reference_atは「いつ話したか」であり、対象出来事の一致には用いない。
    if left.when.model_dump(exclude={"reference_at"}) != right.when.model_dump(exclude={"reference_at"}):
        return False
    if left.where is None or right.where is None:
        return False
    if left.where.entity_id is None or left.where != right.where:
        return False
    if left.why is None or right.why is None or left.why != right.why:
        return False
    return True
