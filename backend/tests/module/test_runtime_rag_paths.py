from __future__ import annotations

from unittest.mock import MagicMock

from app.memory.rag_record import MemoryCandidateRecord
from app.privacy.contracts import ScanSuccess


def test_rt_chroma_01_rag_lookup_uses_resolved_chroma_path(
    monkeypatch, tmp_path
) -> None:
    from app.memory import rag_service
    from app.memory.memory_policy import resolved_memory_policy

    chroma_path = tmp_path / "runtime-data" / "chroma"
    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.5]))
    monkeypatch.setattr(rag_service, "query_memories", MagicMock(return_value=[]))

    rag_service.retrieve_prompt_memories(
        "miori",
        "前回の畑の話を教えて",
        resolved_memory_policy(),
        chroma_path=chroma_path,
    )

    rag_service.query_memories.assert_called_once_with(
        "miori",
        [0.5],
        n_results=5,
        chroma_path=chroma_path,
    )


def test_rt_chroma_01_background_task_keeps_resolved_chroma_path_until_store(
    monkeypatch, tmp_path
) -> None:
    from app.memory import rag_service
    from app.memory.memory_policy import resolved_memory_policy

    chroma_path = tmp_path / "runtime-data" / "chroma"
    record = MemoryCandidateRecord(
        id="00000000-0000-4000-8000-000000000052",
        character="miori",
        role="user",
        content="農業日誌: トマトに水やり",
        timestamp="2026-08-07T00:00:00+00:00",
    )
    scanner = MagicMock()
    scanner.scan.return_value = ScanSuccess(())
    task_queue = MagicMock()
    monkeypatch.setattr(
        rag_service,
        "create_memory_candidate_record",
        MagicMock(return_value=record),
    )
    monkeypatch.setattr(
        rag_service,
        "is_long_term_memory_candidate",
        MagicMock(return_value=True),
    )

    rag_service.record_user_memory_candidate(
        "miori",
        record.content,
        resolved_memory_policy(),
        task_queue,
        privacy_scanner=scanner,
        chroma_path=chroma_path,
    )

    task_queue.add_task.assert_called_once_with(
        rag_service._embed_and_store,
        record,
        chroma_path,
    )

    monkeypatch.setattr(rag_service, "embed_text", MagicMock(return_value=[0.5]))
    monkeypatch.setattr(rag_service, "add_memory", MagicMock())
    task, *arguments = task_queue.add_task.call_args.args
    task(*arguments)

    rag_service.add_memory.assert_called_once_with(
        record.character,
        record.id,
        [0.5],
        record.content,
        {
            "character": record.character,
            "role": record.role,
            "timestamp": record.timestamp,
        },
        chroma_path=chroma_path,
    )
