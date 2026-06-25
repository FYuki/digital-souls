import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "conversations.db"


@dataclass(frozen=True)
class ConversationRecord:
    id: int
    character: str
    role: str
    content: str
    timestamp: str


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
        """
    )


def _to_record(row: sqlite3.Row) -> ConversationRecord:
    return ConversationRecord(
        id=row["id"],
        character=row["character"],
        role=row["role"],
        content=row["content"],
        timestamp=row["timestamp"],
    )


def save_message(character: str, role: str, content: str) -> ConversationRecord:
    timestamp = datetime.now(UTC).isoformat()
    with _connect() as connection:
        _ensure_schema(connection)
        cursor = connection.execute(
            """
            INSERT INTO conversations (character, role, content, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (character, role, content, timestamp),
        )
        record_id = cursor.lastrowid
        if record_id is None:
            raise RuntimeError("Conversation insert did not return an id")
        return ConversationRecord(record_id, character, role, content, timestamp)


def get_messages(character: str, limit: int | None = None) -> list[ConversationRecord]:
    with _connect() as connection:
        _ensure_schema(connection)
        if limit is None:
            rows = connection.execute(
                """
                SELECT id, character, role, content, timestamp
                FROM conversations
                WHERE character = ?
                ORDER BY id ASC
                """,
                (character,),
            ).fetchall()
            return [_to_record(row) for row in rows]

        rows = connection.execute(
            """
            SELECT id, character, role, content, timestamp
            FROM conversations
            WHERE character = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (character, limit),
        ).fetchall()
        return [_to_record(row) for row in reversed(rows)]
