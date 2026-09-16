"""自己申告に限るUI訂正と、全形成経路の監査・削除を提供する。"""

import json
from typing import Protocol
from uuid import UUID

from app.memory.episodic.privacy import PrivacyAssessmentUnavailable
from app.memory.episodic.repository import RecordConflict
from app.memory.providers import MemoryCorrectionRejected
from app.memory.semantic.contracts import FormationType, SemanticStatus
from app.memory.semantic.repository import SemanticConflict
from app.memory.semantic.service import SemanticRejected, SemanticStore


class IndexPurger(Protocol):
    def delete_after_commit(self, *, character_id: str, memory_id: UUID) -> None: ...


class SemanticMemoryManagement:
    def __init__(self, store: SemanticStore, index: IndexPurger) -> None:
        self.store, self.index = store, index

    def list(self, *, character_id: str) -> list[dict[str, object]]:
        self.store.reconcile(character_id)
        with self.store.repository.read() as tx:
            records = tx.list_records(character_id)
            relations = tx.relations(character_id)
            output = []
            for record in records:
                item = record.model_dump(mode="json")
                available = record.status not in {SemanticStatus.DELETED, SemanticStatus.INACTIVE}
                item["proposition"] = record.proposition.model_dump(mode="json") if available and record.proposition else None
                item["can_correct"] = bool(available and record.status in {
                    SemanticStatus.ACTIVE, SemanticStatus.CONFLICTED,
                } and record.formation_type is FormationType.DIRECT_EXTRACTION
                    and record.proposition and record.proposition.self_report)
                item["versions"] = [{
                    "content_version": row["content_version"], "sources": json.loads(row["sources"]),
                    "stamp": json.loads(row["stamp"]), "created_at": row["created_at"],
                    "content_erased": row["proposition"] is None,
                } for row in tx.connection.execute(
                    "SELECT * FROM semantic_versions WHERE character_id=? AND record_id=? ORDER BY content_version",
                    (character_id, str(record.id)),
                )]
                item["relations"] = [r.model_dump(mode="json") for r in relations
                                     if record.id in {r.source_id, r.target_id}]
                item["reassessment_pending"] = bool(tx.connection.execute(
                    "SELECT 1 FROM semantic_reassessment WHERE character_id=? AND record_id=? AND completed_at IS NULL",
                    (character_id, str(record.id)),
                ).fetchone())
                output.append(item)
            return output

    def get(self, *, character_id: str, record_id: UUID) -> dict[str, object]:
        item = next((r for r in self.list(character_id=character_id) if r["id"] == str(record_id)), None)
        if item is None:
            raise LookupError("semantic memory was not found")
        return item

    def correct(
        self, *, character_id: str, record_id: UUID, version: int, value: str, key: UUID,
    ) -> dict[str, object]:
        with self.store.repository.read() as tx:
            current = tx.get(character_id, record_id)
            if current is None:
                raise LookupError("semantic memory was not found")
            if current.proposition is None:
                raise RecordConflict("semantic memory changed")
            previous = current.proposition
            # UIは命題の値を訂正する。所有者・形成方法・自己申告フラグは変更させない。
            proposition = previous.model_copy(update={
                "value": value, "content": f"{previous.subject}の{previous.predicate}は{value}",
            })
        try:
            saved = self.store.correct(character_id=character_id, record_id=record_id, version=version,
                                       proposition=proposition, receipt_id=key)
        except SemanticConflict as error:
            raise RecordConflict("semantic memory changed") from error
        except PrivacyAssessmentUnavailable as error:
            raise MemoryCorrectionRejected(reason_code="SEMANTIC_PRIVACY_UNAVAILABLE") from error
        except SemanticRejected as error:
            raise MemoryCorrectionRejected(reason_code="SEMANTIC_CORRECTION_DENIED") from error
        self.index.delete_after_commit(character_id=character_id, memory_id=record_id)
        return self.get(character_id=character_id, record_id=saved.id)

    def delete(self, *, character_id: str, record_id: UUID, version: int) -> None:
        try:
            self.store.delete(character_id=character_id, record_id=record_id, version=version)
        except SemanticConflict as error:
            raise RecordConflict("semantic memory changed") from error
        self.index.delete_after_commit(character_id=character_id, memory_id=record_id)
