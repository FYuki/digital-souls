from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.memory.embedder import embed_text
from app.memory.index_sync import MemoryIndexSync
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.index_outbox_repository import IndexOutboxRepository
from app.memory.persistence.schema import initialize_persona_memory_schema
from app.runtime_paths import resolve_runtime_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory-index")
    parser.add_argument("command", choices=("worker", "reconcile"))
    arguments = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[3]
    runtime_paths = resolve_runtime_paths(os.environ, repository_root)
    initialize_persona_memory_schema(runtime_paths, repository_root)
    clock = lambda: datetime.now(UTC)
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
        embedder=embed_text,
        clock=clock,
    )
    if arguments.command == "worker":
        sync.run_worker_once()
    else:
        sync.reconcile_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
