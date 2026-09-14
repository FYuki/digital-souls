"""元履歴とEpisodeを同じsnapshotで検証し、古い根拠を知識にしない。"""

from datetime import datetime
import sqlite3

from app.memory.episodic.read_repository import EpisodicReadRepository
from app.memory.episodic.repository import EpisodicTransaction
from app.memory.episodic.sources import InvalidConversationSource, validate_conversation_sources
from app.memory.persistence.contracts import MemoryStatus
from app.memory.semantic.contracts import SemanticSource
from app.memory.semantic.repository import SemanticConflict, SemanticTransaction


def validate_sources(
    history: sqlite3.Connection, cutoff: datetime, tx: SemanticTransaction, *,
    character_id: str, sources: tuple[SemanticSource, ...], episode_reader: EpisodicReadRepository,
) -> tuple[str, ...]:
    texts: list[str] = []
    for source in sources:
        if source.kind == "CONVERSATION":
            assert source.span is not None and source.conversation_id is not None
            validate_conversation_sources(
                history, character_id=character_id, conversation_id=source.conversation_id,
                sources=(source.span,), cutoff=cutoff,
            )
            row = history.execute(
                """SELECT user_content,assistant_content FROM conversation_turns
                   WHERE character_id=? AND conversation_id=? AND turn_id=?""",
                (character_id, str(source.conversation_id), str(source.source_id)),
            ).fetchone()
            if row is None:
                raise InvalidConversationSource("semantic source is unavailable")
            body = str(row["user_content"] if source.span.role == "user" else row["assistant_content"])
            # 出典の一部に保存拒否や機微情報がある場合も元発言全体をprivacy再評価する。
            texts.append(body)
        elif source.kind == "EPISODE":
            episode_tx = EpisodicTransaction(tx.connection, cutoff, readonly=True)
            record = episode_reader.get_in_snapshot(
                episode_tx, history, cutoff, character_id=character_id, memory_id=source.source_id,
            )
            if (record is None or record.memory_type.value != "EPISODE"
                    or record.status is not MemoryStatus.ACTIVE or record.content_version != source.revision):
                raise SemanticConflict("semantic Episode evidence is unavailable or stale")
            texts.append(record.normalized_text)
    return tuple(texts)


def overlaps(left: SemanticSource, right: SemanticSource) -> bool:
    if (left.kind, left.source_id, left.revision) != (right.kind, right.source_id, right.revision):
        return False
    if left.span is None or right.span is None:
        return True
    return (left.conversation_id == right.conversation_id and left.span.role == right.span.role
            and left.span.start < right.span.end and right.span.start < left.span.end)


def is_masked(sources: tuple[SemanticSource, ...], masks: tuple[SemanticSource, ...]) -> bool:
    # 新たな明示発言を根拠にする再取得は可能。古い補助文脈だけを新規根拠に数えない。
    roots = tuple(s for s in sources if s.kind != "CONVERSATION" or (s.span and s.span.role == "user"))
    return bool(roots) and all(any(overlaps(source, mask) for mask in masks) for source in roots)
