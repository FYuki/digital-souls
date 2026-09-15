import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from tests.unit.test_approved_memory_repository import _candidate, _context


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _prepare_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, object]:
    from app.memory import index_cli
    from app.memory.persistence.approved_repository import ApprovedMemoryRepository
    from app.memory.persistence.schema import initialize_persona_memory_schema
    from app.runtime_paths import resolve_runtime_paths

    data_root = tmp_path / "runtime-data"
    monkeypatch.setenv("DS_ENVIRONMENT_ID", "test")
    monkeypatch.setenv("DS_DATA_DIR", str(data_root))
    repository_root = Path(index_cli.__file__).resolve().parents[3]
    paths = resolve_runtime_paths(dict(index_cli.os.environ), repository_root)
    initialize_persona_memory_schema(paths, repository_root)
    repository = ApprovedMemoryRepository(
        database_path=paths.persona_memory_sqlite_path,
        clock=lambda: NOW,
        uuid_factory=uuid4,
        outbox_uuid_factory=uuid4,
    )
    return paths.persona_memory_sqlite_path, repository


def _install_index_double(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[tuple[str, str], dict[str, object]]:
    from app.memory import index_cli, index_sync

    records: dict[tuple[str, str], dict[str, object]] = {}

    class FakeRuntime:
        router = object()
        settings = object()

        def close(self) -> None:
            return None

    class FakeEmbedder:
        provider_id = "ollama"
        model_id = "nomic-embed-text:latest"

        def __init__(self, **_kwargs: object) -> None:
            return None

        def __call__(self, _text: str) -> list[float]:
            return [0.1]

    def upsert(**entry: object) -> None:
        records[(str(entry["character_id"]), str(entry["memory_id"]))] = dict(entry)

    monkeypatch.setattr(index_sync, "upsert_memory_index_entry", upsert)
    monkeypatch.setattr(
        index_sync,
        "delete_memory_index_entry",
        lambda **entry: records.pop(
            (str(entry["character_id"]), str(entry["memory_id"])), None
        ),
    )
    monkeypatch.setattr(
        index_sync,
        "list_memory_index_ids",
        lambda *, character_id, chroma_path, fingerprint=None: {
            memory_id for owner, memory_id in records if owner == character_id
        },
    )
    monkeypatch.setattr(
        index_sync,
        "get_memory_index_metadata",
        lambda *, character_id, memory_id, chroma_path, fingerprint=None: None,
    )
    monkeypatch.setattr(
        index_cli, "create_inference_runtime", lambda _environment: FakeRuntime()
    )
    monkeypatch.setattr(index_cli, "MemoryInferenceEmbedder", FakeEmbedder)
    return records


def test_cli_worker_processes_outbox_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.memory import index_cli

    database_path, repository = _prepare_runtime(tmp_path, monkeypatch)
    records = _install_index_double(monkeypatch)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    assert index_cli.main(["worker"]) == 0

    with sqlite3.connect(database_path) as connection:
        status = connection.execute(
            "SELECT status FROM memory_index_outbox"
        ).fetchone()[0]
    assert status == "COMPLETED"
    assert (
        records[("miori", str(memory.id))]["normalized_text"] == memory.normalized_text
    )


def test_cli_reconcile_rebuilds_missing_index_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.memory import index_cli

    _database_path, repository = _prepare_runtime(tmp_path, monkeypatch)
    records = _install_index_double(monkeypatch)
    memory = repository.save(
        character_id="miori", candidate=_candidate(), context=_context()
    )

    assert index_cli.main(["reconcile"]) == 0

    assert (
        records[("miori", str(memory.id))]["normalized_text"] == memory.normalized_text
    )


def test_cli_rejects_pending_restore_before_schema_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.memory import index_cli

    class RestorePendingError(Exception):
        pass

    observed: list[Path] = []

    def reject_restore(marker_path: Path) -> None:
        observed.append(marker_path)
        raise RestorePendingError

    monkeypatch.setattr(index_cli, "require_no_restore_intent", reject_restore)
    monkeypatch.setattr(
        index_cli,
        "initialize_persona_memory_schema",
        lambda *_args: pytest.fail("復元保留中にスキーマを初期化してはならない"),
    )

    with pytest.raises(RestorePendingError):
        index_cli.main(["worker"])

    assert len(observed) == 1


@pytest.mark.parametrize("command", ["worker", "reconcile"])
def test_cli_indexes_semantic_memory_and_reconciles_invalidated_source(tmp_path, monkeypatch, command):
    from datetime import timedelta
    from types import SimpleNamespace
    from app.memory import index_cli
    from app.memory.episodic.read_repository import EpisodicReadRepository
    from app.memory.episodic.repository import EpisodicRepository
    from app.memory.episodic.sources import ConversationSourceGuard
    from app.memory.semantic.repository import SemanticRepository
    from app.memory.semantic.service import SemanticStore
    from app.runtime_paths import resolve_runtime_paths
    from tests.conversation_history_test_support import create_repository
    from tests.module.test_semantic_store import Reviewer, candidate, source

    database_path, _ = _prepare_runtime(tmp_path, monkeypatch)
    root = Path(index_cli.__file__).resolve().parents[3]
    paths = resolve_runtime_paths(dict(index_cli.os.environ), root)
    history = create_repository(paths.sqlite_path, now=NOW, uuid_factory=uuid4)
    context = SimpleNamespace(paths=paths, history=history, conversation=history.create_conversation("miori"))
    guard = ConversationSourceGuard(paths.sqlite_path, clock=lambda: NOW, retention=timedelta(days=365))
    episodic = EpisodicReadRepository(EpisodicRepository(database_path), guard)
    store = SemanticStore(repository=SemanticRepository(database_path), source_guard=guard,
                          episode_reader=episodic, reviewer=Reviewer(), clock=lambda: NOW)
    evidence = source(context)
    memory = store.save(character_id="miori", candidate=candidate(evidence), receipt_key="semantic-cli")
    records = _install_index_double(monkeypatch)

    assert index_cli.main([command]) == 0
    assert records[("miori", str(memory.id))]["normalized_text"] == memory.proposition.content
    with sqlite3.connect(paths.sqlite_path) as connection:
        connection.execute("UPDATE conversation_turns SET user_content='訂正' WHERE turn_id=?",
                           (str(evidence.source_id),))
    assert index_cli.main(["reconcile"]) == 0
    assert ("miori", str(memory.id)) not in records
    with store.repository.read() as tx:
        assert tx.get("miori", memory.id).status.value == "INACTIVE"
