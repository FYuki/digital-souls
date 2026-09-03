import re
import unicodedata


DEFAULT_CONVERSATION_TITLE = "新しい会話"
CONVERSATION_TITLE_MAX_LENGTH = 40
_SENTENCE_END = re.compile(r"[。！？.!?]")


def generate_conversation_title(user_content: str) -> str:
    """保存可能なユーザー本文から決定論的な初期タイトルを生成する。"""

    normalized = " ".join(unicodedata.normalize("NFC", user_content).split())
    if not normalized:
        return DEFAULT_CONVERSATION_TITLE

    sentence_end = _SENTENCE_END.search(normalized)
    candidate = (
        normalized[: sentence_end.end()]
        if sentence_end is not None
        else normalized
    )
    if len(candidate) <= CONVERSATION_TITLE_MAX_LENGTH:
        return candidate
    return candidate[: CONVERSATION_TITLE_MAX_LENGTH - 1] + "…"


def normalize_manual_conversation_title(title: str) -> str:
    normalized = unicodedata.normalize("NFC", title).strip()
    if not normalized:
        raise ValueError("title must not be empty")
    if len(normalized) > CONVERSATION_TITLE_MAX_LENGTH:
        raise ValueError(
            f"title must be at most {CONVERSATION_TITLE_MAX_LENGTH} characters"
        )
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("title must not contain control characters")
    return normalized
