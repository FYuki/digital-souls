from __future__ import annotations

from unittest.mock import MagicMock


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
