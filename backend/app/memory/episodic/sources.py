"""会話由来の出典を、現在の履歴と照合する。本文を例外やログへ出さない。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from uuid import UUID

from app.conversation_history._sqlite import SqliteSession, format_datetime, parse_datetime
from app.memory.episodic.contracts import SourceSpan


class InvalidConversationSource(ValueError):
    pass


def validate_conversation_sources(
    connection: sqlite3.Connection, *, character_id: str, conversation_id: UUID,
    sources: tuple[SourceSpan, ...], cutoff: datetime,
) -> None:
    """呼び出し側が保持する履歴transaction内で範囲・版・話者・日時を検証する。"""
    if not sources:
        raise InvalidConversationSource("conversation sources are required")
    for source in sources:
        if source.role not in {"user", "assistant"}:
            raise InvalidConversationSource("unsupported conversation source role")
        row = connection.execute(
            """SELECT t.user_content,t.assistant_content,t.created_at,t.updated_at
               FROM conversation_turns t JOIN memory_turn_versions v
               ON v.character_id = t.character_id AND v.conversation_id = t.conversation_id
               AND v.turn_id = t.turn_id
               WHERE t.character_id = ? AND t.conversation_id = ? AND t.turn_id = ?
               AND t.status = 'completed' AND t.created_at >= ? AND v.revision = ? AND v.deleted = 0
               AND NOT EXISTS(SELECT 1 FROM screen_turn_provenance p WHERE p.turn_id = t.turn_id)""",
            (character_id, str(conversation_id), str(source.source_id),
             format_datetime(cutoff), source.revision),
        ).fetchone()
        if row is None:
            raise InvalidConversationSource("conversation source is unavailable")
        body = row["user_content"] if source.role == "user" else row["assistant_content"]
        timestamp = row["created_at"] if source.role == "user" else row["updated_at"]
        if (
            not isinstance(body, str) or source.end > len(body)
            or source.stated_at != parse_datetime(timestamp)
        ):
            raise InvalidConversationSource("conversation source range or timestamp is invalid")


class ConversationSourceGuard:
    def __init__(
        self, database_path: Path, *, clock: Callable[[], datetime], retention: timedelta,
    ) -> None:
        if retention <= timedelta(0):
            raise ValueError("source retention must be positive")
        self._database = SqliteSession(database_path, sqlite3.connect)
        self._clock = clock
        self._retention = retention

    @contextmanager
    def guard(
        self, *, character_id: str, conversation_id: UUID, sources: tuple[SourceSpan, ...],
    ) -> Iterator[sqlite3.Connection]:
        # 履歴 -> 記憶の順でlockする。LLM・Embedding等の外部処理はこの外で行う。
        with self._database.transaction() as connection:
            validate_conversation_sources(
                connection, character_id=character_id, conversation_id=conversation_id,
                sources=sources, cutoff=self._clock() - self._retention,
            )
            yield connection
