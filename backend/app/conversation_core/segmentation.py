from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class TextSegment:
    text: str
    text_range: tuple[int, int]


_SENTENCE_BOUNDARIES = frozenset("。！？!?\n")
_CLAUSE_BOUNDARIES = frozenset("、，,；;：:")
_CLOSERS = frozenset("」』）)]】\"'”’")
_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


class JapaneseTextSegmenter:
    """逐次deltaからVOICEVOXへ渡せる確定済み日本語区間を切り出す。"""

    def __init__(self, *, max_chars: int = 80, min_clause_chars: int = 16) -> None:
        if max_chars < 8 or min_clause_chars < 1 or min_clause_chars > max_chars:
            raise ValueError("text segmentation limits are invalid")
        self._max_chars = max_chars
        self._min_clause_chars = min_clause_chars
        self._buffer = ""
        self._offset = 0

    def feed(self, delta: str) -> tuple[TextSegment, ...]:
        if delta:
            self._buffer += delta
        return self._drain(final=False)

    def finish(self) -> tuple[TextSegment, ...]:
        segments = list(self._drain(final=True))
        return tuple(segments)

    def _drain(self, *, final: bool) -> tuple[TextSegment, ...]:
        result: list[TextSegment] = []
        while self._buffer:
            end = self._confirmed_end(final=final)
            if end is None:
                break
            text = self._buffer[:end]
            if text.strip():
                result.append(
                    TextSegment(text=text, text_range=(self._offset, self._offset + end))
                )
            # 空白だけの区間もLLM本文上のoffsetとしては消費済みにする。
            self._offset += end
            self._buffer = self._buffer[end:]
        return tuple(result)

    def _confirmed_end(self, *, final: bool) -> int | None:
        url_ranges = tuple(
            (match.start(), match.end())
            for match in _URL_PATTERN.finditer(self._buffer)
        )
        for index, character in enumerate(self._buffer):
            end = index + 1
            if character in _SENTENCE_BOUNDARIES:
                while end < len(self._buffer) and self._buffer[end] in _CLOSERS:
                    end += 1
                return end
            if (
                character in _CLAUSE_BOUNDARIES
                and end >= self._min_clause_chars
                and not any(start <= index < finish for start, finish in url_ranges)
            ):
                return end
        if len(self._buffer) >= self._max_chars:
            return self._fallback_end()
        if final:
            return len(self._buffer)
        return None

    def _fallback_end(self) -> int:
        candidate = self._max_chars
        for match in _URL_PATTERN.finditer(self._buffer):
            if match.start() < candidate < match.end():
                candidate = match.end()
                break
        if candidate > self._max_chars * 2:
            candidate = self._max_chars
        prefix = self._buffer[:candidate]
        for marker in (" ", "　", "、", ","):
            safe = prefix.rfind(marker)
            if safe >= self._min_clause_chars:
                return safe + 1
        return candidate
