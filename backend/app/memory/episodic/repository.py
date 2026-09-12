"""Episode / FactのSQLite正本。検証済み候補を単一transactionで書き込む。

抽出・privacy・source snapshotの検証は登録サービスの責務であり、この低水準
repositoryをAPIやLLMから直接呼ばない。呼び出し側はsourceの更新境界を保持し、
transaction内で内容・参照・完了receiptをまとめて確定する。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.memory.episodic.contracts import (
    FiveW, FormationStamp, MergeRelation, Record, RecordKind, RecordStatus,
    Reference, SourceSpan,
)
from app.memory.persistence.sqlite import PersonaMemorySqlite, format_datetime, parse_datetime


class RecordConflict(ValueError):
    """対象の版・状態が変わったため、現在の出典から再評価が必要。"""


@dataclass(frozen=True)
class WriteResult:
    record: Record
    result_version: int
    replayed: bool


@dataclass(frozen=True)
class RecordVersion:
    content_version: int
    five_w: FiveW | None
    sources: tuple[SourceSpan, ...]
    stamp: FormationStamp
    created_at: datetime


def _sources_json(sources: tuple[SourceSpan, ...]) -> str:
    if not sources or len({source.identity for source in sources}) != len(sources):
        raise ValueError("nonempty, distinct sources are required")
    # 発言本文をキーにせず、出典ID・版・role・範囲で安定した順序にする。
    ordered = sorted(sources, key=lambda source: tuple(map(str, source.identity)))
    return json.dumps([source.model_dump(mode="json") for source in ordered],
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_sources(value: str) -> tuple[SourceSpan, ...]:
    return tuple(SourceSpan.model_validate(item) for item in json.loads(value))


def _record(row: sqlite3.Row) -> Record:
    return Record(
        id=UUID(row["id"]), kind=RecordKind(row["kind"]), character_id=row["character_id"],
        conversation_id=UUID(row["conversation_id"]) if row["conversation_id"] else None,
        content_version=row["content_version"], status=RecordStatus(row["status"]),
        five_w=FiveW.model_validate_json(row["content"]) if row["content"] is not None else None,
        created_at=parse_datetime(row["created_at"]), updated_at=parse_datetime(row["updated_at"]),
    )


class EpisodicRepository:
    def __init__(self, database_path: Path) -> None:
        self._database = PersonaMemorySqlite(database_path, sqlite3.connect)

    @contextmanager
    def transaction(self, *, now: datetime | None = None) -> Iterator[EpisodicTransaction]:
        with self._database.transaction() as connection:
            yield EpisodicTransaction(connection, now or datetime.now(UTC))

    @contextmanager
    def read(self) -> Iterator[EpisodicTransaction]:
        with self._database.connection() as connection:
            connection.execute("BEGIN")
            try:
                yield EpisodicTransaction(connection, datetime.now(UTC), readonly=True)
            finally:
                connection.rollback()

    def truncate_wal(self) -> None:
        self._database.truncate_wal()


class EpisodicTransaction:
    def __init__(self, connection: sqlite3.Connection, now: datetime, *, readonly: bool = False) -> None:
        self._connection = connection
        self._now = format_datetime(now)
        self._readonly = readonly

    def _writable(self) -> None:
        if self._readonly:
            raise ValueError("read-only episodic transaction")

    def get(self, character_id: str, record_id: UUID) -> Record | None:
        row = self._connection.execute(
            "SELECT * FROM episodic_records WHERE character_id = ? AND id = ?",
            (character_id, str(record_id)),
        ).fetchone()
        return _record(row) if row is not None else None

    def list_character_ids(self) -> set[str]:
        rows = self._connection.execute(
            "SELECT DISTINCT character_id FROM episodic_records"
        ).fetchall()
        return {str(row["character_id"]) for row in rows}

    def list_records(
        self, character_id: str, *, conversation_id: UUID | None = None,
        kind: RecordKind | None = None, active_only: bool = True,
    ) -> tuple[Record, ...]:
        sql = "SELECT * FROM episodic_records WHERE character_id = ?"
        params: list[str] = [character_id]
        if conversation_id is not None:
            sql += " AND conversation_id = ?"
            params.append(str(conversation_id))
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind.value)
        if active_only:
            sql += " AND status = 'ACTIVE'"
        rows = self._connection.execute(sql + " ORDER BY created_at,id", params).fetchall()
        return tuple(_record(row) for row in rows)

    def versions(self, character_id: str, record_id: UUID) -> tuple[RecordVersion, ...]:
        rows = self._connection.execute(
            """SELECT * FROM episodic_versions WHERE character_id = ? AND record_id = ?
               ORDER BY content_version""", (character_id, str(record_id)),
        ).fetchall()
        return tuple(RecordVersion(
            content_version=row["content_version"],
            five_w=FiveW.model_validate_json(row["content"]) if row["content"] else None,
            sources=_parse_sources(row["sources"]), stamp=FormationStamp.model_validate_json(row["stamp"]), created_at=parse_datetime(row["created_at"]),
        ) for row in rows)

    def _replay(
        self, character_id: str, receipt_id: UUID, operation: str,
        record_id: UUID | None = None,
    ) -> WriteResult | None:
        row = self._connection.execute(
            "SELECT * FROM episodic_receipts WHERE character_id = ? AND receipt_key = ?",
            (character_id, str(receipt_id)),
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or (record_id and row["record_id"] != str(record_id)):
            raise RecordConflict("receipt belongs to another operation")
        record = self.get(character_id, UUID(row["record_id"]))
        if record is None:
            raise RecordConflict("receipt record is missing")
        # 最新状態を返す。過去の保存成功を再生して削除本文を復活させない。
        return WriteResult(record, row["result_version"], True)

    def _receipt(self, record: Record, receipt_id: UUID, operation: str) -> None:
        self._connection.execute(
            "INSERT INTO episodic_receipts VALUES (?,?,?,?,?,?)",
            (record.character_id, str(receipt_id), str(record.id), operation,
             record.content_version, self._now),
        )

    def _valid_sources(self, character_id: str, sources: tuple[SourceSpan, ...]) -> str:
        encoded = _sources_json(sources)
        for source in sources:
            if self._connection.execute(
                """SELECT 1 FROM episodic_invalid_sources
                   WHERE character_id = ? AND source_id = ? AND revision = ?""",
                (character_id, str(source.source_id), source.revision),
            ).fetchone() is not None:
                raise RecordConflict("source revision has been invalidated")
        return encoded

    def _version(self, record: Record, sources: tuple[SourceSpan, ...], stamp: FormationStamp) -> None:
        self._connection.execute(
            "INSERT INTO episodic_versions VALUES (?,?,?,?,?,?,?)",
            (record.character_id, str(record.id), record.content_version,
             record.five_w.model_dump_json() if record.five_w else None,
             self._valid_sources(record.character_id, sources), stamp.model_dump_json(), self._now),
        )

    def _outbox(self, record: Record) -> None:
        self._connection.execute(
            """INSERT INTO memory_index_outbox
               (id,memory_id,character_id,operation,status,attempt_count,last_error_code,created_at,updated_at)
               VALUES (?,?,?,?,'PENDING',0,NULL,?,?)""",
            (str(uuid4()), str(record.id), record.character_id,
             "UPSERT" if record.status is RecordStatus.ACTIVE else "DELETE", self._now, self._now),
        )

    def create(
        self, *, character_id: str, conversation_id: UUID | None, kind: RecordKind,
        five_w: FiveW, sources: tuple[SourceSpan, ...], stamp: FormationStamp, receipt_id: UUID,
    ) -> WriteResult:
        self._writable()
        replay = self._replay(character_id, receipt_id, "CREATE")
        if replay is not None:
            if replay.record.kind is not kind or replay.record.conversation_id != conversation_id:
                raise RecordConflict("receipt belongs to another scope")
            return replay
        self._valid_sources(character_id, sources)
        record = Record(
            id=uuid4(), kind=kind, character_id=character_id, conversation_id=conversation_id,
            content_version=1, status=RecordStatus.ACTIVE, five_w=five_w,
            created_at=parse_datetime(self._now), updated_at=parse_datetime(self._now),
        )
        self._connection.execute(
            """INSERT INTO episodic_records
               (id,character_id,kind,conversation_id,content_version,status,content,
                policy_version,classifier_version,model_id,model_digest,prompt_version,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(record.id), character_id, kind.value, str(conversation_id) if conversation_id else None,
             1, record.status.value, five_w.model_dump_json(), stamp.policy_version,
             stamp.classifier_version, stamp.model_id, stamp.model_digest, stamp.prompt_version,
             self._now, self._now),
        )
        self._version(record, sources, stamp)
        self._receipt(record, receipt_id, "CREATE")
        self._outbox(record)
        return WriteResult(record, 1, False)

    def update(
        self, *, character_id: str, record_id: UUID, expected_version: int,
        five_w: FiveW, sources: tuple[SourceSpan, ...], stamp: FormationStamp, receipt_id: UUID,
    ) -> WriteResult:
        self._writable()
        replay = self._replay(character_id, receipt_id, "UPDATE", record_id)
        if replay is not None:
            return replay
        old = self.get(character_id, record_id)
        if old is None or old.status is RecordStatus.DELETED or old.content_version != expected_version:
            raise RecordConflict("record is missing, deleted, or has changed")
        # sourcesは新しい内容全体を裏付ける有効出典。旧出典は過去版の監査情報に残す。
        self._valid_sources(character_id, sources)
        self._connection.execute(
            """UPDATE episodic_records SET content_version = content_version + 1,
               content = ?, status = 'ACTIVE', policy_version = ?, classifier_version = ?,
               model_id = ?, model_digest = ?, prompt_version = ?, updated_at = ?
               WHERE character_id = ? AND id = ?""",
            (five_w.model_dump_json(), stamp.policy_version, stamp.classifier_version,
             stamp.model_id, stamp.model_digest, stamp.prompt_version, self._now, character_id, str(record_id)),
        )
        record = self.get(character_id, record_id)
        assert record is not None
        self._version(record, sources, stamp)
        self._receipt(record, receipt_id, "UPDATE")
        self._outbox(record)
        return WriteResult(record, record.content_version, False)

    def delete(
        self, *, character_id: str, record_id: UUID, expected_version: int,
        sources: tuple[SourceSpan, ...], receipt_id: UUID,
    ) -> WriteResult:
        self._writable()
        replay = self._replay(character_id, receipt_id, "DELETE", record_id)
        if replay is not None:
            return replay
        old = self.get(character_id, record_id)
        if old is None or old.status is RecordStatus.DELETED or old.content_version != expected_version:
            raise RecordConflict("record is missing, deleted, or has changed")
        self._valid_sources(character_id, sources)
        self._connection.execute(
            """UPDATE episodic_records SET content_version = content_version + 1,
               content = NULL, status = 'DELETED', updated_at = ? WHERE character_id = ? AND id = ?""",
            (self._now, character_id, str(record_id)),
        )
        # 監査版にも削除対象本文を残さない。本文を持たないIDと出典・receiptは再生防止に残す。
        self._connection.execute(
            "UPDATE episodic_versions SET content = NULL WHERE character_id = ? AND record_id = ?",
            (character_id, str(record_id)),
        )
        record = self.get(character_id, record_id)
        assert record is not None
        stamp = self.versions(character_id, record_id)[-1].stamp
        self._version(record, sources, stamp)
        self._receipt(record, receipt_id, "DELETE")
        self._outbox(record)
        return WriteResult(record, record.content_version, False)

    def add_reference(
        self, *, character_id: str, episode_id: UUID, episode_version: int,
        fact_id: UUID, fact_version: int, sources: tuple[SourceSpan, ...],
    ) -> Reference:
        self._writable()
        encoded = self._valid_sources(character_id, sources)
        row = self._connection.execute(
            """SELECT id FROM episodic_links WHERE character_id = ? AND episode_id = ?
               AND episode_version = ? AND fact_id = ? AND fact_version = ? AND sources = ? AND valid = 1""",
            (character_id, str(episode_id), episode_version, str(fact_id), fact_version, encoded),
        ).fetchone()
        link = Reference(id=UUID(row["id"]) if row else uuid4(), character_id=character_id,
                         episode_id=episode_id, episode_version=episode_version, fact_id=fact_id,
                         fact_version=fact_version, sources=sources, valid=True)
        if row is None:
            self._connection.execute(
                """INSERT INTO episodic_links
                   (id,character_id,episode_id,episode_version,fact_id,fact_version,sources,valid,created_at)
                   VALUES (?,?,?,?,?,?,?,1,?)""",
                (str(link.id), character_id, str(episode_id), episode_version,
                 str(fact_id), fact_version, encoded, self._now),
            )
        return link

    def references(self, character_id: str, episode_id: UUID) -> tuple[Reference, ...]:
        rows = self._connection.execute(
            "SELECT * FROM episodic_links WHERE character_id = ? AND episode_id = ? ORDER BY created_at,id",
            (character_id, str(episode_id)),
        ).fetchall()
        return tuple(Reference(
            id=UUID(row["id"]), character_id=row["character_id"], episode_id=UUID(row["episode_id"]),
            episode_version=row["episode_version"], fact_id=UUID(row["fact_id"]),
            fact_version=row["fact_version"], sources=_parse_sources(row["sources"]), valid=bool(row["valid"]),
        ) for row in rows)

    def add_merge(self, relation: MergeRelation) -> None:
        self._writable()
        self._connection.execute(
            """INSERT INTO episodic_merges
               (id,character_id,source_fact_id,source_version,target_fact_id,target_version,
                conversation_id,policy,evidence,valid,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (str(relation.id), relation.character_id, str(relation.source_fact_id),
             relation.source_version, str(relation.target_fact_id), relation.target_version,
             str(relation.conversation_id), relation.policy, self._valid_sources(relation.character_id, relation.evidence),
             int(relation.valid), self._now),
        )

    def merges(self, character_id: str) -> tuple[MergeRelation, ...]:
        rows = self._connection.execute(
            "SELECT * FROM episodic_merges WHERE character_id = ? ORDER BY created_at,id",
            (character_id,),
        ).fetchall()
        return tuple(MergeRelation(
            id=UUID(row["id"]), character_id=row["character_id"], source_fact_id=UUID(row["source_fact_id"]),
            source_version=row["source_version"], target_fact_id=UUID(row["target_fact_id"]),
            target_version=row["target_version"], conversation_id=UUID(row["conversation_id"]),
            policy=row["policy"], evidence=_parse_sources(row["evidence"]), valid=bool(row["valid"]),
        ) for row in rows)

    def representative(self, character_id: str, fact_id: UUID) -> Record | None:
        seen: set[UUID] = set()
        current_id = fact_id
        while current_id not in seen:
            seen.add(current_id)
            current = self.get(character_id, current_id)
            if current is None or current.kind is not RecordKind.FACT or current.status is not RecordStatus.ACTIVE:
                return None
            row = self._connection.execute(
                """SELECT m.* FROM episodic_merges m JOIN episodic_records t
                   ON t.character_id = m.character_id AND t.id = m.target_fact_id
                   WHERE m.character_id = ? AND m.source_fact_id = ? AND m.valid = 1
                   AND m.source_version = ? AND t.content_version = m.target_version AND t.status = 'ACTIVE'""",
                (character_id, str(current_id), current.content_version),
            ).fetchone()
            if row is None:
                return current
            current_id = UUID(row["target_fact_id"])
        raise RecordConflict("cyclic merge relations")

    def invalidate_source(self, character_id: str, source_id: UUID, revision: int) -> tuple[UUID, ...]:
        """指定旧版に依存する正本・参照・統合関係を停止する。新しい版には触れない。"""
        self._writable()
        if isinstance(revision, bool) or revision < 1:
            raise ValueError("source revision must be positive")
        params = (character_id, str(source_id), revision)
        self._connection.execute(
            "INSERT OR IGNORE INTO episodic_invalid_sources VALUES (?,?,?,?)",
            (*params, self._now),
        )
        rows = self._connection.execute(
            """SELECT DISTINCT r.id FROM episodic_records r JOIN episodic_versions v
               ON r.character_id = v.character_id AND r.id = v.record_id
               AND r.content_version = v.content_version, json_each(v.sources) s
               WHERE r.character_id = ? AND r.status = 'ACTIVE'
               AND json_extract(s.value,'$.source_id') = ? AND json_extract(s.value,'$.revision') = ?""",
            params,
        ).fetchall()
        ids = tuple(UUID(row["id"]) for row in rows)
        for record_id in ids:
            self._connection.execute(
                "UPDATE episodic_records SET status = 'INACTIVE', updated_at = ? WHERE character_id = ? AND id = ?",
                (self._now, character_id, str(record_id)),
            )
            record = self.get(character_id, record_id)
            assert record is not None
            self._outbox(record)
        for table, evidence in (("episodic_links", "sources"), ("episodic_merges", "evidence")):
            self._connection.execute(
                f"""UPDATE {table} SET valid = 0 WHERE character_id = ? AND valid = 1
                    AND EXISTS(SELECT 1 FROM json_each({evidence}) s
                      WHERE json_extract(s.value,'$.source_id') = ? AND json_extract(s.value,'$.revision') = ?)""",
                params,
            )
        return ids


    def is_processed(self, character_id: str, conversation_id: UUID, anchor: SourceSpan) -> bool:
        """primary範囲で所有した開始位置を使い、分割境界と引用長の変動で再登録しない。"""
        return self._connection.execute(
            """SELECT 1 FROM episodic_processed_spans WHERE character_id = ? AND conversation_id = ?
               AND source_id = ? AND revision = ? AND role = ? AND start <= ? AND end > ? LIMIT 1""",
            (character_id, str(conversation_id), str(anchor.source_id), anchor.revision,
             anchor.role, anchor.start, anchor.start),
        ).fetchone() is not None

    def mark_processed(
        self, character_id: str, conversation_id: UUID, sources: tuple[SourceSpan, ...],
    ) -> None:
        self._writable()
        for source in sources:
            self._connection.execute(
                "INSERT OR IGNORE INTO episodic_processed_spans VALUES (?,?,?,?,?,?,?,?)",
                (character_id, str(conversation_id), str(source.source_id), source.revision,
                 source.role, source.start, source.end, self._now),
            )


    def processed_ranges(
        self, character_id: str, conversation_id: UUID, source: SourceSpan,
    ) -> tuple[tuple[int, int], ...]:
        rows = self._connection.execute(
            """SELECT start,end FROM episodic_processed_spans
               WHERE character_id = ? AND conversation_id = ? AND source_id = ? AND revision = ?
               AND role = ? AND start < ? AND end > ? ORDER BY start,end""",
            (character_id, str(conversation_id), str(source.source_id), source.revision,
             source.role, source.end, source.start),
        ).fetchall()
        return tuple((int(row["start"]), int(row["end"])) for row in rows)


    def receipt_record(self, character_id: str, receipt_id: UUID) -> Record | None:
        row = self._connection.execute(
            "SELECT record_id FROM episodic_receipts WHERE character_id = ? AND receipt_key = ?",
            (character_id, str(receipt_id)),
        ).fetchone()
        return self.get(character_id, UUID(row["record_id"])) if row else None
