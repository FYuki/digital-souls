"""保存済み会話と処理済み位置を正本にし、未取得の発言を回復可能に抽出する。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import sqlite3
from time import monotonic
from typing import Literal
from uuid import UUID

from app.conversation_history._sqlite import TURN_COLUMNS, turn_from_row
from app.memory.episodic.contracts import ExtractionIdentity
from app.memory.formation.thread_chunks import ThreadFragment
from app.memory.formation.thread_queue import ThreadSource
from app.memory.semantic.contracts import SemanticStatus
from app.memory.semantic.extractor import InputPart
from app.memory.semantic.pipeline import SemanticDeferred, SemanticPipeline
from app.memory.semantic.service import SemanticStore


@dataclass(frozen=True)
class PendingSource:
    primary: ThreadSource
    context: tuple[ThreadSource, ...]

    @property
    def identity(self) -> tuple[UUID, int]:
        return self.primary.turn.turn_id, self.primary.revision


class SemanticWorkQueue:
    def __init__(self, store: SemanticStore) -> None:
        self.store = store

    def peek(self, *, exclude: frozenset[tuple[UUID, int]] = frozenset()) -> PendingSource | None:
        with self.store.source_guard.snapshot() as (history, cutoff), self.store.repository.read() as tx:
            from app.conversation_history._sqlite import format_datetime
            rows = history.execute(
                f"""SELECT {', '.join('t.'+c.strip() for c in TURN_COLUMNS.split(','))},v.revision
                    FROM conversation_turns t JOIN memory_turn_versions v
                    ON v.character_id=t.character_id AND v.conversation_id=t.conversation_id AND v.turn_id=t.turn_id
                    WHERE t.status='completed' AND v.deleted=0 AND t.created_at>=?
                    AND NOT EXISTS(SELECT 1 FROM screen_turn_provenance p WHERE p.turn_id=t.turn_id)
                    ORDER BY t.created_at,t.rowid""", (format_datetime(cutoff),),
            ).fetchall()
            previous: dict[tuple[str, UUID], list[ThreadSource]] = {}
            for row in rows:
                source = ThreadSource(turn_from_row(row), row["revision"])
                turn = source.turn
                key = turn.character_id, turn.conversation_id
                context = previous.setdefault(key, [])
                if source.turn.user_content and (turn.turn_id, source.revision) not in exclude and not tx.processed(
                    turn.character_id, turn.conversation_id, turn.turn_id, source.revision,
                ):
                    return PendingSource(source, tuple(context[-2:]))
                context.append(source)
            return None

    def has_pending(self) -> bool:
        return self.peek() is not None


def input_batches(pending: PendingSource, *, max_characters: int = 4000) -> tuple[tuple[InputPart, ...], ...]:
    if max_characters < 1:
        raise ValueError("semantic input size must be positive")
    context: list[InputPart] = []
    for index, source in enumerate(pending.context):
        messages: tuple[tuple[Literal["user", "assistant"], str | None], ...] = (
            ("user", source.turn.user_content), ("assistant", source.turn.assistant_content),
        )
        for role, text in messages:
            if text:
                context.append(InputPart(
                    f"c{index}:{role}", ThreadFragment(source, role, max(0, len(text)-1000), len(text)), False,
                ))
    primary = pending.primary
    text = primary.turn.user_content or ""
    return tuple(tuple([*context, InputPart(
        f"u{index}", ThreadFragment(primary, "user", start, min(start+max_characters, len(text))), True,
    )]) for index, start in enumerate(range(0, len(text), max(1, max_characters - min(400, max_characters // 4)))))


class SemanticWorker:
    def __init__(
        self, *, queue: SemanticWorkQueue, pipeline: SemanticPipeline,
        identity: Callable[[], ExtractionIdentity], priority_available: Callable[[], bool],
        retry_seconds: float = 10,
    ) -> None:
        self.queue, self.pipeline = queue, pipeline
        self.identity, self.priority_available = identity, priority_available
        self.retry_seconds = retry_seconds
        self.backoff: dict[tuple[UUID, int], float] = {}

    def process_next(self, *, should_stop: Callable[[], bool]) -> bool:
        if should_stop() or not self.priority_available():
            return False
        # sourceの削除・編集は通常の増分位置とは独立に失効させる。
        with self.queue.store.repository.read() as tx:
            characters = [str(row[0]) for row in tx.connection.execute("SELECT DISTINCT character_id FROM semantic_records")]
        for character in characters:
            self.queue.store.reconcile(character)
        self.backoff = {key: until for key, until in self.backoff.items() if until > monotonic()}
        pending = self.queue.peek(exclude=frozenset(self.backoff))
        if pending is None:
            return False
        turn = pending.primary.turn
        records = self.queue.store.reconcile(turn.character_id)
        catalog = tuple(r for r in records if r.status in {SemanticStatus.ACTIVE, SemanticStatus.CONFLICTED})
        if should_stop() or not self.priority_available():
            return False
        try:
            self.pipeline.process(
                character_id=turn.character_id, conversation_id=turn.conversation_id,
                source_id=turn.turn_id, revision=pending.primary.revision,
                batches=input_batches(pending), catalog=catalog, extraction=self.identity(),
                should_defer=lambda: should_stop() or not self.priority_available(),
            )
        except SemanticDeferred:
            return False
        except Exception:
            self.backoff[pending.identity] = monotonic() + self.retry_seconds
            raise
        return True


def conversation_idle(
    history: sqlite3.Connection, *, now: datetime, quiet_seconds: float = 5,
) -> bool:
    """別スレッドを含む会話処理を優先し、会話直後の連続入力にも短い猶予を置く。"""
    from app.conversation_history._sqlite import parse_datetime
    row = history.execute(
        "SELECT SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END),MAX(updated_at) FROM conversation_turns"
    ).fetchone()
    if row is None:
        return True
    if row[0]:
        return False
    return row[1] is None or (now-parse_datetime(row[1])).total_seconds() >= quiet_seconds
