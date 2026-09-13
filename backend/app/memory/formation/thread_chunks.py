"""有効なスレッド本文を欠落なく分割し、重なり部分の候補所有範囲を明示する。"""

from dataclasses import dataclass
from typing import Literal

import hashlib
import json
from app.memory.formation.thread_queue import ThreadSnapshot, ThreadSource


@dataclass(frozen=True)
class ThreadFragment:
    source: ThreadSource
    role: Literal["user", "assistant"]
    start: int
    end: int

    @property
    def text(self) -> str:
        content = (
            self.source.turn.user_content
            if self.role == "user"
            else self.source.turn.assistant_content
        )
        assert content is not None
        return content[self.start : self.end]


@dataclass(frozen=True)
class ThreadChunk:
    index: int
    primary: tuple[ThreadFragment, ...]
    context_before: tuple[ThreadFragment, ...]
    context_after: tuple[ThreadFragment, ...]

    @property
    def fragments(self) -> tuple[ThreadFragment, ...]:
        return self.context_before + self.primary + self.context_after

    @property
    def key(self) -> str:
        identities = [
                (str(p.source.turn.turn_id), p.source.revision, p.role, p.start, p.end)
                for p in self.fragments
            ]
        return hashlib.sha256(json.dumps(identities, separators=(",", ":")).encode()).hexdigest()

    def owns_user_quote(self, source: ThreadSource, start: int, quote: str) -> bool:
        """引用の開始位置で所有chunkを一つに決め、周辺contextだけの二重抽出を拒否する。"""
        if (
            type(start) is not int
            or start < 0
            or not quote
            or not (source.turn.user_content or "").startswith(quote, start)
        ):
            return False
        owned = any(
            part.source == source
            and part.role == "user"
            and part.start <= start < part.end
            for part in self.primary
        )
        if not owned:
            return False
        # 引用の全文がこの入力に見えていることも確認する。分割境界を越える引用は許可する。
        cursor = start
        for part in self.fragments:
            if (
                part.source == source
                and part.role == "user"
                and part.start <= cursor < part.end
            ):
                cursor = part.end
                if cursor >= start + len(quote):
                    return True
        return False


def split_thread(
    snapshot: ThreadSnapshot,
    *,
    max_characters: int = 12000,
    context_characters: int = 1000,
) -> tuple[ThreadChunk, ...]:
    if (
        type(max_characters) is not int
        or type(context_characters) is not int
        or max_characters < 1
        or context_characters < 0
    ):
        raise ValueError(
            "chunk limits must be nonnegative integers with positive primary budget"
        )
    spans: list[tuple[int, int, ThreadSource, Literal["user", "assistant"]]] = []
    total = 0
    for source in snapshot.sources:
        parts: tuple[tuple[Literal["user", "assistant"], str | None], ...] = (
            ("user", source.turn.user_content),
            ("assistant", source.turn.assistant_content),
        )
        for role, content in parts:
            if content:
                spans.append((total, total + len(content), source, role))
                total += len(content)

    def fragments(start: int, end: int) -> tuple[ThreadFragment, ...]:
        return tuple(
            ThreadFragment(
                source, role, max(start, lower) - lower, min(end, upper) - lower
            )
            for lower, upper, source, role in spans
            if lower < end and upper > start
        )

    return tuple(
        ThreadChunk(
            index,
            fragments(start, min(total, start + max_characters)),
            fragments(max(0, start - context_characters), start),
            fragments(
                min(total, start + max_characters),
                min(total, start + max_characters + context_characters),
            ),
        )
        for index, start in enumerate(range(0, total, max_characters))
    )


def bisect_chunk(chunk: ThreadChunk) -> tuple[ThreadChunk, ThreadChunk]:
    """primaryを二分し、周辺contextも縮める。所有範囲は欠落・重複させない。"""
    total = sum(p.end - p.start for p in chunk.primary)
    if total < 2:
        raise ValueError("minimum chunk cannot fit model budget")
    half = total // 2

    def slice_parts(
        parts: tuple[ThreadFragment, ...], lower: int, upper: int
    ) -> tuple[ThreadFragment, ...]:
        offset = 0
        result = []
        for p in parts:
            size = p.end - p.start
            start, end = max(0, lower - offset), min(size, upper - offset)
            if start < end:
                result.append(
                    ThreadFragment(p.source, p.role, p.start + start, p.start + end)
                )
            offset += size
        return tuple(result)

    left = slice_parts(chunk.primary, 0, half)
    right = slice_parts(chunk.primary, half, total)
    context_size = max(1, half // 4)
    before = chunk.context_before + left
    after = right + chunk.context_after
    before_size = sum(p.end - p.start for p in before)
    original_before_size = sum(p.end - p.start for p in chunk.context_before)
    return (
        ThreadChunk(
            chunk.index,
            left,
            slice_parts(
                chunk.context_before,
                max(0, original_before_size - context_size),
                original_before_size,
            ),
            slice_parts(after, 0, context_size),
        ),
        ThreadChunk(
            chunk.index,
            right,
            slice_parts(before, max(0, before_size - context_size), before_size),
            slice_parts(chunk.context_after, 0, context_size),
        ),
    )
