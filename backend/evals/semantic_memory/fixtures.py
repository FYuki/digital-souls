"""評価専用DBへ固定の合成会話と既存知識を準備する。本番データは参照しない。"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from uuid import uuid4

from app.conversation_history.models import ProcessingTurnInput
from app.conversation_history.repository import ConversationHistoryRepository
from app.conversation_history.schema import initialize_conversation_history_schema
from app.conversation_history.wal_cleanup import ConversationWalCleanup
from app.memory.episodic.contracts import FiveW, FormationStamp, RecordKind, SourceSpan, What
from app.memory.episodic.read_repository import EpisodicReadRepository
from app.memory.episodic.repository import EpisodicRepository
from app.memory.episodic.sources import ConversationSourceGuard
from app.memory.formation.thread_queue import ThreadSource
from app.memory.persistence.schema import initialize_persona_memory_schema
from app.memory.semantic.contracts import FormationType, Proposition, SemanticCandidate, SemanticSource
from app.memory.semantic.repository import SemanticRepository
from app.memory.semantic.service import SemanticStore
from app.memory.semantic.worker import PendingSource
from app.runtime_paths import resolve_runtime_paths

NOW = datetime(2026, 9, 15, tzinfo=UTC)
FIXTURE_STAMP = FormationStamp(policy_version="fixture", classifier_version="fixture",
                               model_id="fixture", model_digest="fixture", prompt_version="fixture")


class Fixture:
    def __init__(self, root: Path, reviewer):
        self.root = root
        repository = root / "repository"
        repository.mkdir()
        self.paths = resolve_runtime_paths(
            {"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(root / "data")}, repository,
        )
        initialize_persona_memory_schema(self.paths, repository)
        initialize_conversation_history_schema(self.paths.sqlite_path)
        self.history = ConversationHistoryRepository(
            database_path=self.paths.sqlite_path, clock=lambda: NOW, uuid_factory=uuid4,
            stale_after=timedelta(minutes=5), retention=timedelta(days=365),
            wal_cleanup=ConversationWalCleanup(database_path=self.paths.sqlite_path,
                                                clock=lambda: NOW, connection_factory=sqlite3.connect),
        )
        self.episodic = EpisodicRepository(self.paths.persona_memory_sqlite_path)
        self.repository = SemanticRepository(self.paths.persona_memory_sqlite_path)
        guard = ConversationSourceGuard(self.paths.sqlite_path, clock=lambda: NOW, retention=timedelta(days=365))
        self.store = SemanticStore(repository=self.repository, source_guard=guard,
                                   episode_reader=EpisodicReadRepository(self.episodic, guard),
                                   reviewer=reviewer, clock=lambda: NOW)
        self.known = {}
        self.turn_indexes = {}

    def turn(self, character, conversation_id, user, assistant="わかりました。"):
        turn = self.history.create_processing_turn(character, conversation_id, ProcessingTurnInput(user))
        turn = self.history.complete_turn(character, conversation_id, turn.turn_id,
                                          sanitized_assistant_content=assistant)
        with sqlite3.connect(self.paths.sqlite_path) as connection:
            revision = connection.execute("SELECT revision FROM memory_turn_versions WHERE turn_id=?",
                                           (str(turn.turn_id),)).fetchone()[0]
        return ThreadSource(turn, revision)

    @staticmethod
    def evidence(source):
        turn = source.turn
        return SemanticSource(kind="CONVERSATION", source_id=turn.turn_id, revision=source.revision,
            conversation_id=turn.conversation_id,
            span=SourceSpan(source_id=turn.turn_id, revision=source.revision, role="user",
                            start=0, end=len(turn.user_content), stated_at=turn.created_at))

    def prepare(self, data):
        for raw in data.get("existing", []):
            character = raw.get("character_id", "miori")
            conversation = self.history.create_conversation(character)
            formation = FormationType(raw["formation_type"])
            sources = []
            count = 2 if formation is FormationType.EXPERIENCE_DERIVED else 1
            for _ in range(count):
                source = self.turn(character, conversation.conversation_id, raw["content"])
                evidence = self.evidence(source)
                with self.repository.transaction(now=NOW) as tx:
                    tx.mark_processed(character, conversation.conversation_id, source.turn.turn_id, source.revision)
                if formation is FormationType.EXPERIENCE_DERIVED:
                    with self.episodic.transaction(now=NOW) as tx:
                        result = tx.create(character_id=character, conversation_id=conversation.conversation_id,
                            kind=RecordKind.EPISODE, five_w=FiveW(what=What(predicate="聞いた", object=raw["content"])),
                            sources=(evidence.span,), stamp=FIXTURE_STAMP, receipt_id=uuid4())
                    sources.append(SemanticSource(kind="EPISODE", source_id=result.record.id, revision=1))
                else:
                    sources.append(evidence)
            proposition = Proposition(**{key: raw[key] for key in
                ("subject", "predicate", "value", "content", "mutability", "self_report")})
            # 既知知識は評価入力のfixture。候補の本番保存処理とは別に初期化する。
            with self.repository.transaction(now=NOW) as tx:
                record = tx.apply(character_id=character, stamp=FIXTURE_STAMP,
                    candidate=SemanticCandidate(formation_type=formation, proposition=proposition,
                                                sources=tuple(sources), confidence=0.9),
                    receipt_key=f"fixture:{raw['key']}")
            self.known[raw["key"]] = record
        conversation = self.history.create_conversation("miori")
        turns = []
        for index, raw in enumerate(data["turns"]):
            source = self.turn("miori", conversation.conversation_id, raw["user"], raw["assistant"])
            turns.append(source)
            self.turn_indexes[str(source.turn.turn_id)] = index
        return PendingSource(turns[-1], tuple(turns[:-1]))
