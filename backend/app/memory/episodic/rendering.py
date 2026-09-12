"""表示・検索用の投影。仮定・創作・欠損・申告の区別を本文に残す。"""

from app.memory.episodic.contracts import FiveW, NarrativeContext, PersonRole, RecordKind
from app.memory.episodic.temporal import render_time

_CONTEXT = {
    NarrativeContext.REPORTED: "申告・観測された内容",
    NarrativeContext.HYPOTHETICAL: "仮定の話",
    NarrativeContext.FICTIONAL: "創作の話",
    NarrativeContext.UNKNOWN: "実際の出来事か不明",
}
_ROLE = {
    PersonRole.ACTOR: "行為者",
    PersonRole.PARTICIPANT: "参加者",
    PersonRole.SPEAKER: "話し手",
    PersonRole.LISTENER: "聞き手",
    PersonRole.TOPIC: "話題の人物",
}
_POLARITY = {"AFFIRMED": "肯定", "NEGATED": "否定", "UNKNOWN": "肯否不明"}
_ACTUALITY = {
    "OCCURRED": "実施したとの内容", "PLANNED": "予定",
    "CONDITIONAL": "条件付き", "UNKNOWN": "実施・予定は不明",
}


def render_five_w(value: FiveW, *, kind: RecordKind) -> str:
    title = "経験" if kind is RecordKind.EPISODE else "取得した情報"
    people = "、".join(f"{_ROLE[p.role]}:{p.name}" for p in value.who) or "不明"
    return " / ".join((
        f"{title}（{_CONTEXT[value.context]}）",
        f"人物:{people}",
        f"内容:{value.what.predicate}",
        f"対象:{value.what.object or '不明'}",
        f"肯否:{_POLARITY[value.what.polarity]}",
        f"実施・予定:{_ACTUALITY[value.what.actuality]}",
        f"日時:{render_time(value.when)}",
        f"場所:{value.where.name if value.where else '不明'}",
        f"理由:{value.why or '不明'}",
    ))
