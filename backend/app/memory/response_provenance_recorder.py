"""生成へ渡した参照を、回答保存より先に記録するruntime境界。"""

from dataclasses import replace
from pathlib import Path
import sqlite3
from uuid import UUID

from app.conversation_history.models import ConversationTurn
from app.conversation_history.prompt_history import RestoredHistoryTurn
from app.memory.episodic.read_repository import EpisodicReadRepository
from app.memory.episodic.repository import EpisodicRepository, EpisodicTransaction
from app.memory.episodic.response_provenance import record_response
from app.memory.persistence.sqlite import PersonaMemorySqlite, format_datetime
from app.prompting.models import BuiltPrompt


class ResponseProvenanceRecorder:
    def __init__(self, database_path: Path, *, reader: EpisodicReadRepository | None = None) -> None:
        self._reader = reader
        self._database = PersonaMemorySqlite(database_path, sqlite3.connect)
        self._repository = EpisodicRepository(database_path)

    def record(self, turn: ConversationTurn, prompt: BuiltPrompt) -> None:
        references = []
        for message in prompt.messages:
            reference = message.memory_reference
            if reference is not None:
                if reference.content_version is None:
                    raise ValueError("selected memory has no content version")
                references.append((UUID(reference.memory_id), reference.content_version))
        parents = tuple(
            message.history_turn_id for message in prompt.messages
            if message.history_turn_id is not None
        )
        with self._database.transaction() as connection:
            record_response(
                connection, character_id=turn.character_id, conversation_id=turn.conversation_id,
                turn_id=turn.turn_id, references=references, history_turn_ids=parents,
                created_at=format_datetime(turn.created_at),
            )
            invalid = turn.turn_id in EpisodicTransaction(
                connection, turn.created_at,
            ).invalid_response_ids(turn.character_id)
        # 生成中の訂正・削除でも、HTTP応答の確定前に失効を検知する。
        # 依存metadataは確定済みとして残し、失敗した履歴の後処理へ渡す。
        if self._reader is not None:
            invalid |= turn.turn_id in self._reader.invalid_response_ids(turn.character_id)
        if invalid:
            raise ValueError("response depends on an invalid memory version")

    def filter_history(self, character_id: str, turn: RestoredHistoryTurn) -> RestoredHistoryTurn:
        if turn.turn_id is None or turn.assistant_content is None:
            return turn
        if self._reader is not None:
            invalid = turn.turn_id in self._reader.invalid_response_ids(character_id)
        else:
            with self._repository.read() as tx:
                invalid = turn.turn_id in tx.invalid_response_ids(character_id)
        # 元の履歴本文は書き換えず、生成へ渡す投影から失効した回答を除く。
        return replace(turn, assistant_content=None) if invalid else turn
