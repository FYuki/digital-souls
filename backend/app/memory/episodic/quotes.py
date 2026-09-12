"""引用を元発言の版とUnicode文字範囲へ結び付ける。"""

from app.memory.episodic.contracts import SourceSpan
from app.memory.episodic.extraction_contracts import SourceQuote
from app.memory.formation.thread_chunks import ThreadChunk, ThreadFragment
from app.memory.formation.thread_queue import ThreadSnapshot


class InvalidExtraction(ValueError):
    pass


def fragment_span(fragment: ThreadFragment) -> SourceSpan:
    turn = fragment.source.turn
    return SourceSpan(
        source_id=turn.turn_id, revision=fragment.source.revision, role=fragment.role,
        start=fragment.start, end=fragment.end,
        stated_at=turn.created_at if fragment.role == "user" else turn.updated_at,
    )


def resolve_quote(quote: SourceQuote, snapshot: ThreadSnapshot, chunk: ThreadChunk) -> SourceSpan:
    source = next((s for s in snapshot.sources
                   if s.turn.turn_id == quote.source_id and s.revision == quote.revision), None)
    if source is None:
        raise InvalidExtraction("unknown source revision")
    body = source.turn.user_content if quote.role == "user" else source.turn.assistant_content
    if body is None:
        raise InvalidExtraction("source body is unavailable")
    start = quote.start
    if start is None:
        start = body.find(quote.quote)
        if start < 0 or body.find(quote.quote, start + 1) >= 0:
            raise InvalidExtraction("quote is missing or ambiguous")
    if not body.startswith(quote.quote, start):
        raise InvalidExtraction("quote does not match its source")
    end = start + len(quote.quote)
    cursor = start
    parts = sorted((p for p in chunk.fragments if p.source == source and p.role == quote.role),
                   key=lambda p: p.start)
    for part in parts:
        if part.start <= cursor < part.end:
            cursor = part.end
            if cursor >= end:
                break
    if cursor < end:
        raise InvalidExtraction("quote was not visible in extraction input")
    return SourceSpan(
        source_id=quote.source_id, revision=quote.revision, role=quote.role, start=start, end=end,
        stated_at=source.turn.created_at if quote.role == "user" else source.turn.updated_at,
    )


def owns_anchor(chunk: ThreadChunk, anchor: SourceSpan) -> bool:
    return any(
        p.source.turn.turn_id == anchor.source_id and p.source.revision == anchor.revision
        and p.role == anchor.role and p.start <= anchor.start < p.end
        for p in chunk.primary
    )


def distinct_sources(sources: tuple[SourceSpan, ...]) -> tuple[SourceSpan, ...]:
    return tuple({source.identity: source for source in sources}.values())
