from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.inference.runtime import create_inference_runtime
from app.memory.inference_client import MemoryInferenceEmbedder
from app.memory.index_sync import MemoryIndexSync
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.index_outbox_repository import IndexOutboxRepository
from app.memory.persistence.schema import initialize_persona_memory_schema
from app.restore_intent import require_no_restore_intent
from app.runtime_paths import resolve_runtime_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-index")
    parser.add_argument("command", choices=("worker", "reconcile"))
    arguments = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    runtime_paths = resolve_runtime_paths(os.environ, repository_root)
    require_no_restore_intent(runtime_paths.restore_intent_path)
    initialize_persona_memory_schema(runtime_paths, repository_root)
    clock = lambda: datetime.now(UTC)
    inference_runtime = create_inference_runtime(os.environ)
    try:
        embedder = MemoryInferenceEmbedder(
            router=inference_runtime.router,
            settings=inference_runtime.settings,
        )
        sync = MemoryIndexSync(
            approved_repository=ApprovedMemoryRepository(
                database_path=runtime_paths.persona_memory_sqlite_path,
                clock=clock,
                uuid_factory=uuid4,
                outbox_uuid_factory=uuid4,
            ),
            outbox_repository=IndexOutboxRepository(
                database_path=runtime_paths.persona_memory_sqlite_path,
                clock=clock,
            ),
            chroma_path=runtime_paths.chroma_path,
            runtime_report_dir=runtime_paths.runtime_report_dir,
            embedder=embedder,
            embedding_provider_id=embedder.provider_id,
            embedding_model_id=embedder.model_id,
            clock=clock,
        )
        if arguments.command == "worker":
            sync.run_worker_once()
        else:
            sync.reconcile_once()
    finally:
        inference_runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
