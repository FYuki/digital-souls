from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
from typing import Any, Literal, TypeAlias, cast
import unicodedata

from app.inference import InferenceCaller, InferenceMessage, InferenceTarget

ScreenReferenceDecisionValue = Literal[
    "answer_without_screen", "inspect_screen", "clarify_reference"
]
ScreenReferenceBasis = Literal[
    "current_message", "recent_conversation", "shared_screen_candidate",
    "competing_references", "insufficient_context",
]
ScreenReferencePath = Literal["rule", "llm", "fallback"]
ScreenReferenceProvenance = Literal["none", "current_session", "expired_session"]

REFERENCE_DECISION_SCHEMA: Mapping[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "basis"],
    "properties": {
        "decision": {"type": "string", "enum": [
            "answer_without_screen", "inspect_screen", "clarify_reference"
        ]},
        "basis": {"type": "string", "enum": [
            "current_message", "recent_conversation", "shared_screen_candidate",
            "competing_references", "insufficient_context",
        ]},
    },
    "allOf": [
        {
            "if": {"properties": {"decision": {"const": "clarify_reference"}}},
            "then": {"properties": {"basis": {"enum": [
                "competing_references", "insufficient_context"
            ]}}},
        },
        {
            "if": {"properties": {"decision": {"const": "inspect_screen"}}},
            "then": {"properties": {"basis": {"enum": [
                "current_message", "shared_screen_candidate"
            ]}}},
        },
    ],
}

_TARGET = re.compile(r"(?:この|今|現在の)?(?:画面|スクリーン|モニター|ウィンドウ|表示内容)")
_PERCEPTION = re.compile(
    r"(?:見(?:て|える|せて)|確認(?:して|できる)|読(?:んで|める)|"
    r"説明(?:して|できる)|教えて|何(?:が|を).{0,8}(?:表示|映)|"
    r"(?:表示|映)(?:って|されて).{0,8}(?:いる|る|ます))"
)
_NEGATION = re.compile(
    r"(?:見(?:ない|なくて|なくても|ないで)|確認(?:しない|しなくて|不要)|"
    r"読(?:まない|まなくて)|説明(?:しない|不要)|参照(?:しない|不要))"
)
_PAST_ONLY = re.compile(r"(?:見た|確認した|読んだ|表示されていた|映っていた)")
_FEATURE_TALK = re.compile(
    r"(?:機能|仕様|使い方|設定|実装|API|ボタン|権限).{0,12}(?:について|とは|を)|"
    r"(?:について|とは).{0,12}(?:機能|仕様|使い方|設定|実装|API|ボタン|権限)"
)
_QUOTED_SPAN = re.compile(r"「[^」]*」|『[^』]*』|\"[^\"]*\"")
_DEICTIC = re.compile(r"(?:これ|ここ|今見えている|この表示|この警告)")
_FOLLOWUP = re.compile(r"(?:それ|その|さっきの|先ほどの)")
_FRESHNESS = re.compile(r"(?:今(?:は|も)|現在|直った|消えた|変わった|その下|小さい文字|細かい文字)")
_ADVICE = re.compile(r"(?:どう直す|直し方|どうすれば|対処|詳しく|意味)")
_LOCATION = re.compile(r"(?:右上|左上|右下|左下|上|下|中央|真ん中|右側|左側|小さい文字)")
_PASTED = re.compile(r"(?:^|\n)(?:Error|Exception|Traceback|エラー|https?://|```|\w+Error:)")
_COMPETING = re.compile(
    r"(?:貼った|本文|エラー|会話|説明).{0,30}(?:画面|見えている|警告)|"
    r"(?:画面|見えている|警告).{0,30}(?:貼った|本文|エラー|会話|説明)|"
    r"(?:どっち|どちら)"
)


@dataclass(frozen=True, repr=False)
class ScreenReferenceHistoryItem:
    role: Literal["user", "assistant"]
    content: str
    screen_provenance: ScreenReferenceProvenance = "none"


@dataclass(frozen=True)
class ScreenReferenceDecision:
    decision: ScreenReferenceDecisionValue
    basis: ScreenReferenceBasis
    path: ScreenReferencePath
    normalized_text: str
    target_hint: str | None = None
    inherit_screen_provenance: bool = False
    screen_candidate: bool = False
    unavailable_reason: str | None = None

    @property
    def requested(self) -> bool:
        """旧explicit判定利用箇所との移行期間用互換property。"""
        return self.decision == "inspect_screen"


StructuredReferenceRouter: TypeAlias = Any


def detect_screen_reference(text: str) -> ScreenReferenceDecision:
    """履歴を必要としない、明確な現在画面要求だけを判定する。"""
    normalized = _normalize(text)
    analysis_text = _analysis_text(normalized)
    requested = _is_explicit_screen_request(analysis_text)
    return ScreenReferenceDecision(
        "inspect_screen" if requested else "answer_without_screen",
        "current_message", "rule", normalized,
        _target_hint(analysis_text) if requested else None,
    )


def needs_reference_history(text: str) -> bool:
    """参照候補がなく、履歴判定自体を省略できる発言かを返す。"""
    analysis_text = _analysis_text(_normalize(text))
    return any(
        pattern.search(analysis_text) is not None
        for pattern in (_DEICTIC, _FOLLOWUP, _FRESHNESS, _COMPETING)
    )


def decide_screen_reference(
    text: str,
    *,
    sharing_active: bool,
    screen_use_authorized: bool,
    explicit_ui: bool = False,
    history: Iterable[ScreenReferenceHistoryItem] = (),
    router: StructuredReferenceRouter | None = None,
    cloud_judge_history_allowed: bool = True,
) -> ScreenReferenceDecision:
    """画像取得前に、許可済み会話文脈だけから3分岐を決める。"""
    normalized = _normalize(text)
    analysis_text = _analysis_text(normalized)
    recent = tuple(history)[-4:]
    if not analysis_text or _NEGATION.search(analysis_text):
        return _decision("answer_without_screen", "current_message", normalized)
    screen_candidate = _is_screen_candidate(
        analysis_text, explicit_ui=explicit_ui, raw_text=text
    )
    if not sharing_active or not screen_use_authorized:
        return _decision(
            "answer_without_screen", "current_message", normalized,
            screen_candidate=screen_candidate,
        )
    if _FEATURE_TALK.search(analysis_text):
        return _decision("answer_without_screen", "current_message", normalized)
    if _PAST_ONLY.search(analysis_text) and not _FRESHNESS.search(analysis_text):
        return _decision("answer_without_screen", "recent_conversation", normalized)
    if explicit_ui or _is_explicit_screen_request(analysis_text):
        return _decision("inspect_screen", "current_message", normalized,
                         target_hint=_target_hint(analysis_text))
    if _PASTED.search(unicodedata.normalize("NFKC", text)) and _DEICTIC.search(analysis_text):
        return _decision("answer_without_screen", "current_message", normalized)
    if _COMPETING.search(analysis_text):
        return _judge_competing(normalized, recent, router=router,
                                history_allowed=cloud_judge_history_allowed)
    if _FRESHNESS.search(analysis_text) and recent:
        return _decision("inspect_screen", "shared_screen_candidate", normalized,
                         target_hint=_target_hint(analysis_text))
    if _FOLLOWUP.search(analysis_text) and any(
        item.screen_provenance == "expired_session" for item in recent
    ):
        return _decision("clarify_reference", "insufficient_context", normalized,
                         path="fallback")
    if _ADVICE.search(analysis_text) and recent:
        return _decision(
            "answer_without_screen", "recent_conversation", normalized,
            inherit_screen_provenance=any(
                item.screen_provenance != "none" for item in recent
            ),
        )
    if _FOLLOWUP.search(analysis_text) and recent:
        return _decision(
            "answer_without_screen", "recent_conversation", normalized,
            inherit_screen_provenance=any(
                item.screen_provenance != "none" for item in recent
            ),
        )
    if _DEICTIC.search(analysis_text) or _LOCATION.search(analysis_text):
        return _decision("inspect_screen", "shared_screen_candidate", normalized,
                         target_hint=_target_hint(analysis_text))
    return _decision("answer_without_screen", "current_message", normalized)


def _judge_competing(
    current_message: str,
    history: tuple[ScreenReferenceHistoryItem, ...],
    *,
    router: StructuredReferenceRouter | None,
    history_allowed: bool,
) -> ScreenReferenceDecision:
    if not history_allowed and any(item.screen_provenance != "none" for item in history):
        return _decision("clarify_reference", "insufficient_context", current_message,
                         path="fallback")
    if router is None:
        return _decision("clarify_reference", "competing_references", current_message,
                         path="fallback")
    messages = (
        InferenceMessage(
            "system",
            "画面画像を見ず、現在発言が会話内対象と共有画面のどちらを指すかだけを"
            "指定schemaで判定してください。対象、権限、Providerは選ばないでください。",
        ),
        InferenceMessage(
            "user",
            "直近会話:\n" + "\n".join(
                f"{item.role}: {item.content}" for item in history
            ) + f"\n現在発言: {current_message}",
        ),
    )
    try:
        router.estimate_input_tokens(
            caller=InferenceCaller.SCREEN_REFERENCE, target=InferenceTarget.CHAT,
            messages=messages, response_schema=REFERENCE_DECISION_SCHEMA,
            timeout_seconds=3.0, max_input_tokens=1536,
        )
        result = router.generate_structured(
            caller=InferenceCaller.SCREEN_REFERENCE, target=InferenceTarget.CHAT,
            messages=messages, response_schema=REFERENCE_DECISION_SCHEMA,
            timeout_seconds=3.0, max_input_tokens=1536, max_output_tokens=64,
        )
        value = cast(Mapping[str, object], result.value)
        return _decision(
            cast(ScreenReferenceDecisionValue, value["decision"]),
            cast(ScreenReferenceBasis, value["basis"]),
            current_message, path="llm",
        )
    except Exception:
        return _decision("clarify_reference", "competing_references", current_message,
                         path="fallback")


def _decision(
    decision: ScreenReferenceDecisionValue,
    basis: ScreenReferenceBasis,
    normalized: str,
    *,
    path: ScreenReferencePath = "rule",
    target_hint: str | None = None,
    inherit_screen_provenance: bool = False,
    screen_candidate: bool = False,
    unavailable_reason: str | None = None,
) -> ScreenReferenceDecision:
    return ScreenReferenceDecision(
        decision, basis, path, normalized, target_hint, inherit_screen_provenance,
        screen_candidate, unavailable_reason,
    )


def _is_screen_candidate(
    text: str, *, explicit_ui: bool, raw_text: str
) -> bool:
    if explicit_ui or _is_explicit_screen_request(text):
        return True
    if _PASTED.search(unicodedata.normalize("NFKC", raw_text)):
        return False
    return any(pattern.search(text) is not None for pattern in (
        _DEICTIC, _LOCATION, _FRESHNESS,
    ))


def _is_explicit_screen_request(text: str) -> bool:
    if not text or _NEGATION.search(text) or _FEATURE_TALK.search(text):
        return False
    if _TARGET.search(text) is None or _PERCEPTION.search(text) is None:
        return False
    return not (
        _PAST_ONLY.search(text) and not re.search(
            r"(?:今|現在|この).{0,8}(?:見て|確認して|読んで|説明して|教えて)", text
        )
    )


def _target_hint(text: str) -> str | None:
    match = _LOCATION.search(text)
    if match is not None:
        return match.group(0)
    for label in ("警告", "エラー", "表示", "文字"):
        if label in text:
            return label
    return None


def _analysis_text(normalized: str) -> str:
    return _normalize(_QUOTED_SPAN.sub(" ", normalized))


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"[\s\u3000]+", " ", normalized).strip()
    return re.sub(r"[、,]+", "、", normalized)
