"""検証済み意味記憶の正本。外部推論はtransactionの外で実行する。"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from app.memory.episodic.contracts import FormationStamp
from app.memory.persistence.sqlite import PersonaMemorySqlite, format_datetime, parse_datetime
from app.memory.semantic.contracts import (
    FormationType, Proposition, SemanticCandidate, SemanticOperation, SemanticRecord,
    SemanticRelation, SemanticSource, SemanticStatus,
)


class SemanticConflict(ValueError):
    """再照合が必要な版・状態・操作の不一致。本文は例外へ含めない。"""


def sources_json(sources: tuple[SemanticSource, ...]) -> str:
    return json.dumps([s.model_dump(mode="json") for s in sources],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def from_row(row: sqlite3.Row) -> SemanticRecord:
    return SemanticRecord(
        id=UUID(row["id"]), character_id=row["character_id"],
        formation_type=FormationType(row["formation_type"]),
        content_version=row["content_version"], status=SemanticStatus(row["status"]),
        proposition=Proposition.model_validate_json(row["proposition"]) if row["proposition"] else None,
        sources=tuple(SemanticSource.model_validate(s) for s in json.loads(row["sources"])),
        confidence=row["confidence"], stamp=FormationStamp.model_validate_json(row["stamp"]),
        created_at=parse_datetime(row["created_at"]), updated_at=parse_datetime(row["updated_at"]),
    )


class SemanticRepository:
    def __init__(self, database_path: Path) -> None:
        self.database = PersonaMemorySqlite(database_path, sqlite3.connect)

    @contextmanager
    def transaction(self, *, now: datetime | None = None) -> Iterator["SemanticTransaction"]:
        with self.database.transaction() as connection:
            yield SemanticTransaction(connection, now or datetime.now(UTC))

    @contextmanager
    def read(self) -> Iterator["SemanticTransaction"]:
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            try:
                yield SemanticTransaction(connection, datetime.now(UTC), readonly=True)
            finally:
                connection.rollback()


class SemanticTransaction:
    def __init__(self, connection: sqlite3.Connection, now: datetime, *, readonly: bool = False) -> None:
        self.connection = connection
        self.now = format_datetime(now)
        self.readonly = readonly

    def _write(self) -> None:
        if self.readonly:
            raise SemanticConflict("read-only semantic transaction")

    def get(self, character_id: str, record_id: UUID) -> SemanticRecord | None:
        row = self.connection.execute(
            "SELECT * FROM semantic_records WHERE character_id=? AND id=?",
            (character_id, str(record_id)),
        ).fetchone()
        return from_row(row) if row is not None else None

    def list_records(self, character_id: str) -> tuple[SemanticRecord, ...]:
        return tuple(from_row(row) for row in self.connection.execute(
            "SELECT * FROM semantic_records WHERE character_id=? ORDER BY created_at,id", (character_id,),
        ))

    def relations(self, character_id: str) -> tuple[SemanticRelation, ...]:
        return tuple(SemanticRelation(
            character_id=row["character_id"], source_id=UUID(row["source_id"]),
            source_version=row["source_version"], target_id=UUID(row["target_id"]),
            target_version=row["target_version"], relation=SemanticOperation(row["relation"]),
            created_at=parse_datetime(row["created_at"]),
        ) for row in self.connection.execute(
            "SELECT * FROM semantic_relations WHERE character_id=? ORDER BY created_at", (character_id,),
        ))

    def replay(self, character_id: str, key: str) -> SemanticRecord | None:
        row = self.connection.execute(
            "SELECT record_id FROM semantic_receipts WHERE character_id=? AND receipt_key=?",
            (character_id, key),
        ).fetchone()
        return self.get(character_id, UUID(row[0])) if row else None

    def _receipt(self, record: SemanticRecord, key: str) -> None:
        if not key.strip():
            raise ValueError("receipt key is required")
        self.connection.execute("INSERT INTO semantic_receipts VALUES(?,?,?,?)",
                                (record.character_id, key, str(record.id), self.now))

    def _version(self, record_id: UUID, character_id: str) -> None:
        self.connection.execute(
            """INSERT INTO semantic_versions
               SELECT character_id,id,content_version,proposition,sources,stamp,updated_at
               FROM semantic_records WHERE character_id=? AND id=?""",
            (character_id, str(record_id)),
        )

    def _outbox(self, character_id: str, record_id: UUID, *, delete: bool = False) -> None:
        self.connection.execute(
            """INSERT INTO memory_index_outbox
               (id,memory_id,character_id,operation,status,attempt_count,created_at,updated_at)
               VALUES(?,?,?,?,'PENDING',0,?,?)""",
            (str(uuid4()), str(record_id), character_id, "DELETE" if delete else "UPSERT", self.now, self.now),
        )

    def apply(
        self, *, character_id: str, candidate: SemanticCandidate, stamp: FormationStamp,
        receipt_key: str, operation: SemanticOperation = SemanticOperation.NEW,
        target_id: UUID | None = None, target_version: int | None = None,
    ) -> SemanticRecord:
        """source/privacy検証を済ませ、履歴guard保持中に呼び出す内部保存入口。"""
        self._write()
        replay = self.replay(character_id, receipt_key)
        if replay is not None:
            return replay
        if (operation is SemanticOperation.NEW) != (target_id is None):
            raise SemanticConflict("operation and target do not agree")
        target = self.get(character_id, target_id) if target_id else None
        if target_id is not None:
            if (target is None or target.content_version != target_version or target.proposition is None
                    or target.status in {SemanticStatus.DELETED, SemanticStatus.INACTIVE,
                                         SemanticStatus.SUPERSEDED, SemanticStatus.HISTORICAL}):
                raise SemanticConflict("semantic target is unavailable or stale")
            if (target.proposition.subject != candidate.proposition.subject
                    or target.proposition.predicate != candidate.proposition.predicate):
                raise SemanticConflict("semantic target has another subject or predicate")
        if operation is SemanticOperation.REAFFIRM:
            assert target is not None and target.proposition is not None
            if (target.status is not SemanticStatus.ACTIVE
                    or target.formation_type != candidate.formation_type
                    or target.proposition != candidate.proposition):
                raise SemanticConflict("reaffirmation cannot change content or formation type")
            all_sources = tuple({s.identity: s for s in (*target.sources, *candidate.sources)}.values())
            self.connection.execute(
                """UPDATE semantic_records SET content_version=content_version+1,sources=?,updated_at=?
                   WHERE character_id=? AND id=?""",
                (sources_json(all_sources), self.now, character_id, str(target.id)),
            )
            self._version(target.id, character_id)
            record = self.get(character_id, target.id)
            assert record is not None
            self._receipt(record, receipt_key)
            self._outbox(character_id, record.id)
            return record
        if operation is SemanticOperation.CHANGE:
            assert target is not None and target.proposition is not None
            if target.proposition.mutability != "CHANGEABLE" or candidate.proposition.mutability != "CHANGEABLE":
                raise SemanticConflict("fixed attributes cannot silently change")
        if operation is SemanticOperation.SELF_REPORT:
            assert target is not None
            pair = {(target.formation_type, bool(target.proposition and target.proposition.self_report)),
                    (candidate.formation_type, candidate.proposition.self_report)}
            if pair != {(FormationType.DIRECT_EXTRACTION, True), (FormationType.EXPERIENCE_DERIVED, False)}:
                raise SemanticConflict("self-report priority requires direct report and generalization")
        record_id = uuid4()
        status = SemanticStatus.CONFLICTED if operation is SemanticOperation.CONFLICT else SemanticStatus.ACTIVE
        self.connection.execute(
            "INSERT INTO semantic_records VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (str(record_id), character_id, candidate.formation_type.value, 1, status.value,
             candidate.proposition.model_dump_json(), sources_json(candidate.sources),
             candidate.confidence, stamp.model_dump_json(), self.now, self.now),
        )
        self._version(record_id, character_id)
        if target is not None:
            previous_status = {
                SemanticOperation.CORRECT: SemanticStatus.SUPERSEDED,
                SemanticOperation.CHANGE: SemanticStatus.HISTORICAL,
                SemanticOperation.CONFLICT: SemanticStatus.CONFLICTED,
            }.get(operation)
            if previous_status is not None:
                self._status(character_id, target.id, previous_status)
            self.connection.execute(
                "INSERT INTO semantic_relations VALUES(?,?,?,?,?,?,?)",
                (character_id, str(target.id), target.content_version,
                 str(record_id), 1, operation.value, self.now),
            )
        result = self.get(character_id, record_id)
        assert result is not None
        self._receipt(result, receipt_key)
        self._outbox(character_id, record_id)
        return result

    def _status(self, character_id: str, record_id: UUID, status: SemanticStatus) -> None:
        self._write()
        self.connection.execute(
            "UPDATE semantic_records SET status=?,updated_at=? WHERE character_id=? AND id=? AND status!='DELETED'",
            (status.value, self.now, character_id, str(record_id)),
        )
        self._outbox(character_id, record_id, delete=status is not SemanticStatus.ACTIVE)

    def invalidate(self, record: SemanticRecord, *, reason: str) -> None:
        self._write()
        if record.status in {SemanticStatus.DELETED, SemanticStatus.INACTIVE}:
            return
        self._status(record.character_id, record.id, SemanticStatus.INACTIVE)
        self.connection.execute(
            "INSERT INTO semantic_reassessment VALUES(?,?,?,?,?,?,NULL) ON CONFLICT DO NOTHING",
            (record.character_id, str(record.id), record.content_version,
             record.formation_type.value, reason, self.now),
        )

    def hard_delete(self, character_id: str, record_id: UUID, *, version: int) -> None:
        self._write()
        record = self.get(character_id, record_id)
        if record is None:
            raise LookupError("semantic record was not found")
        if record.status is SemanticStatus.DELETED:
            return
        if record.content_version != version:
            raise SemanticConflict("semantic deletion target is stale")
        rows = self.connection.execute(
            "SELECT sources FROM semantic_versions WHERE character_id=? AND record_id=?",
            (character_id, str(record_id)),
        ).fetchall()
        for row in rows:
            for source in json.loads(row[0]):
                self.connection.execute(
                    "INSERT INTO semantic_deletion_masks VALUES(?,?,?) ON CONFLICT DO NOTHING",
                    (character_id, str(record_id), json.dumps(source, sort_keys=True, separators=(",", ":"))),
                )
        self.connection.execute(
            "UPDATE semantic_records SET status='DELETED',proposition=NULL,updated_at=? WHERE character_id=? AND id=?",
            (self.now, character_id, str(record_id)),
        )
        self.connection.execute(
            "UPDATE semantic_versions SET proposition=NULL WHERE character_id=? AND record_id=?",
            (character_id, str(record_id)),
        )
        self._outbox(character_id, record_id, delete=True)

    def processed(self, character_id: str, conversation_id: UUID, source_id: UUID, revision: int) -> bool:
        return self.connection.execute(
            """SELECT 1 FROM semantic_processed_sources
               WHERE character_id=? AND conversation_id=? AND source_id=? AND revision=?""",
            (character_id, str(conversation_id), str(source_id), revision),
        ).fetchone() is not None

    def mark_processed(self, character_id: str, conversation_id: UUID, source_id: UUID, revision: int) -> None:
        self._write()
        self.connection.execute(
            "INSERT INTO semantic_processed_sources VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
            (character_id, str(conversation_id), str(source_id), revision, self.now),
        )

    def masks(self, character_id: str) -> tuple[SemanticSource, ...]:
        return tuple(SemanticSource.model_validate_json(row[0]) for row in self.connection.execute(
            "SELECT source FROM semantic_deletion_masks WHERE character_id=?", (character_id,),
        ))
