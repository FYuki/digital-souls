from __future__ import annotations

import re
from typing import Literal


TurnDecision = Literal["backchannel", "take_turn", "indeterminate"]


_BACKCHANNELS = frozenset(
    {
        "うん",
        "うんうん",
        "はい",
        "ええ",
        "へえ",
        "へー",
        "ふうん",
        "ふーん",
        "ほう",
        "なるほど",
        "たしかに",
        "確かに",
        "そうなんだ",
        "そうなの",
        "そっか",
        "そうか",
        "そうそう",
        "わかる",
        "分かる",
    }
)
_BACKCHANNEL_SEPARATORS = re.compile(r"[\s、。！？!?…・]+")


def classify_turn(transcript: str) -> TurnDecision:
    """短い同意・反応だけを相槌とみなし、それ以外は発話権取得と判定する。"""
    normalized = _BACKCHANNEL_SEPARATORS.sub("", transcript.strip())
    if not normalized:
        return "indeterminate"
    if normalized in _BACKCHANNELS:
        return "backchannel"
    return "take_turn"
