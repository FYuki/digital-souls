"""固定合成記憶の準備と実参照の照合。空状態の第1段階検査とは分離する。"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import UUID

from app.memory.admission.contracts import ApprovedMemoryCandidate, PreferencePolarity, UserPreferenceValue
from app.memory.memory_policy import resolved_memory_policy
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.contracts import FormationMethod, MemorySourceInput, MemorySourceType, MemoryWriteContext
from app.memory.persistence.schema import PERSONA_MEMORY_TABLES, initialize_persona_memory_schema
from app.runtime_paths import RuntimePaths, resolve_runtime_paths
from app.voice_measurement_memory import POLICY_PATH

PROFILE = "integration-irodori-memory-reference"
FIXTURE_VERSION = "synthetic-greeting-preferences-v1"
FIXTURE = (
    ("こんにちはという日本語の挨拶", "ユーザーは「こんにちは」という日本語の挨拶を好む。"),
    ("穏やかな挨拶", "ユーザーは会話の始まりに穏やかな挨拶を好む。"),
    ("昼の短い挨拶", "ユーザーは昼に短くこんにちはと挨拶することを好む。"),
)
MEMORY_IDS = tuple(UUID(f"35000000-0000-4000-8000-{i:012d}") for i in range(1, 4))
NOW = datetime(2026, 9, 19, tzinfo=UTC)
RECEIPT = Path("voice-metrics/reference-fixture.json")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def owned_paths(data_root: Path, repository_root: Path) -> RuntimePaths:
    """追記先も新規の専用試行だけに限定し、既存rootやsymlinkを拒否する。"""
    paths = resolve_runtime_paths({"DS_ENVIRONMENT_ID": "test", "DS_DATA_DIR": str(data_root)}, repository_root)
    runs = (repository_root / "frontend/test-results/livekit-quality/runs").resolve()
    if (data_root.name != "runtime-data" or data_root.parent.parent.resolve() != runs
            or not re.fullmatch(r"350-memory-reference-[A-Za-z0-9_-]{1,40}", data_root.parent.name)):
        raise ValueError("reference fixture requires its dedicated trial data root")
    return paths


def fixture_identity() -> dict[str, object]:
    return {"fixture_version": FIXTURE_VERSION, "fixture_sha256": digest(FIXTURE),
            "memory_ids": [str(value) for value in MEMORY_IDS], "memory_count": len(FIXTURE)}


def prepare(data_root: Path, repository_root: Path) -> dict[str, object]:
    paths = owned_paths(data_root, repository_root)
    if data_root.exists():
        raise ValueError("reference fixture never overwrites an existing data root")
    initialize_persona_memory_schema(paths, repository_root)
    ids = iter(MEMORY_IDS)
    outbox_ids = iter(UUID(f"35000000-0000-4000-9000-{i:012d}") for i in range(1, 4))
    repository = ApprovedMemoryRepository(database_path=paths.persona_memory_sqlite_path,
        clock=lambda: NOW, uuid_factory=lambda: next(ids), outbox_uuid_factory=lambda: next(outbox_ids))
    policy = resolved_memory_policy()
    for index, (value, text) in enumerate(FIXTURE):
        repository.save(character_id="miori",
            candidate=ApprovedMemoryCandidate(UserPreferenceValue(PreferencePolarity.LIKE, value), text),
            context=MemoryWriteContext(formation_method=FormationMethod.DIRECT,
                idempotency_key=f"{FIXTURE_VERSION}:{index}", occurred_at=None,
                occurred_timezone=None, occurred_precision=None, stated_at=NOW, expires_at=None,
                policy_version=policy.policy_version, classifier_version="synthetic-fixture",
                model_id="synthetic-fixture", model_digest=digest(FIXTURE), prompt_version=FIXTURE_VERSION,
                sources=(MemorySourceInput(MemorySourceType.PROVIDER_RECORD, "synthetic-fixture",
                                           f"{FIXTURE_VERSION}:{index}"),)))
    with sqlite3.connect(paths.persona_memory_sqlite_path) as database:
        rows = database.execute("SELECT * FROM approved_memories ORDER BY id").fetchall()
        sources = database.execute("SELECT * FROM memory_sources ORDER BY memory_id").fetchall()
    receipt = {**fixture_identity(), "approved_rows_sha256": digest(rows),
               "source_rows_sha256": digest(sources), "policy_version": policy.policy_version}
    target = data_root / RECEIPT
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x") as output:
        json.dump(receipt, output, sort_keys=True)
    return receipt


def inspect_initial(data_root: Path, repository_root: Path, profile_path: Path, conversation_id: str) -> dict[str, object]:
    paths = owned_paths(data_root, repository_root)
    UUID(conversation_id)
    profile = json.loads(profile_path.read_text())
    environment = profile.get("derivedEnvironment", {})
    identity = json.loads(paths.identity_marker_path.read_text())
    if (identity.get("environmentId") != "test" or profile.get("effectiveProfile") != PROFILE
            or environment.get("DS_ENVIRONMENT_ID") != "test"
            or Path(environment.get("DS_DATA_DIR", "")).resolve() != data_root.resolve()
            or environment.get("RAG_ENABLED") != "true"
            or environment.get("VOICE_MEASUREMENT_DISABLE_MEMORY_FORMATION") != "true"):
        raise ValueError("reference measurement profile or isolation mismatch")
    scheduler = json.loads((data_root / POLICY_PATH).read_text())
    if scheduler != {"method": "controlled_memory_schedulers_v1",
                     "formation_disabled": True, "consolidation_disabled": True}:
        raise ValueError("reference measurement requires disabled formation and consolidation")
    receipt = json.loads((data_root / RECEIPT).read_text())
    if any(receipt.get(key) != value for key, value in fixture_identity().items()):
        raise ValueError("reference fixture identity mismatch")
    with sqlite3.connect(paths.sqlite_path.as_uri() + "?mode=ro", uri=True) as history:
        history.execute("BEGIN")
        if history.execute("SELECT archived_at FROM conversations WHERE character_id=? AND conversation_id=?",
                           ("miori", conversation_id)).fetchall() != [(None,)]:
            raise ValueError("reference measurement needs an active empty conversation")
        if (history.execute("SELECT count(*) FROM conversation_turns").fetchone()[0]
                or history.execute("SELECT count(*) FROM screen_turn_provenance").fetchone()[0]):
            raise ValueError("reference measurement requires empty history")
    # 派生indexの準備だけを待つ。失敗は即時検出し、試行やSessionを自動再試行しない。
    deadline = time.monotonic() + 120
    while True:
        with sqlite3.connect(paths.persona_memory_sqlite_path.as_uri() + "?mode=ro", uri=True) as check:
            statuses = check.execute("SELECT status FROM memory_index_outbox").fetchall()
        if any(row[0] == "FAILED" for row in statuses):
            raise ValueError("reference index preparation failed")
        if all(row[0] == "COMPLETED" for row in statuses):
            break
        if time.monotonic() >= deadline:
            raise ValueError("reference index preparation timed out")
        time.sleep(0.2)
    with sqlite3.connect(paths.persona_memory_sqlite_path.as_uri() + "?mode=ro", uri=True) as memory:
        memory.execute("BEGIN")
        rows = memory.execute("SELECT * FROM approved_memories ORDER BY id").fetchall()
        sources = memory.execute("SELECT * FROM memory_sources ORDER BY memory_id").fetchall()
        if digest(rows) != receipt["approved_rows_sha256"] or digest(sources) != receipt["source_rows_sha256"]:
            raise ValueError("fixed memory content changed")
        counts = {table: memory.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                  for table in sorted(PERSONA_MEMORY_TABLES)}
        nonempty = {"approved_memories", "memory_sources", "memory_write_receipts", "memory_index_outbox"}
        if any(count != (3 if table in nonempty else 0) for table, count in counts.items()):
            raise ValueError("unexpected memory or provenance rows")
        pending = memory.execute("SELECT count(*) FROM memory_index_outbox WHERE status != 'COMPLETED'").fetchone()[0]
        if pending:
            raise ValueError("reference index preparation incomplete")
    from app.memory.chroma_store import active_memory_index_fingerprint, list_memory_index_ids
    fingerprint = active_memory_index_fingerprint("miori", paths.chroma_path)
    if fingerprint is None:
        raise ValueError("reference index fingerprint missing")
    index_ids = list_memory_index_ids(character_id="miori", chroma_path=paths.chroma_path, fingerprint=fingerprint)
    if set(index_ids) != set(receipt["memory_ids"]):
        raise ValueError("reference index contents mismatch")
    evidence = {"method": "sqlite_fixed_reference_state_v1", "rag_enabled": True,
                "fixture_version": FIXTURE_VERSION, "fixture_sha256": digest(FIXTURE),
                "approved_rows_sha256": receipt["approved_rows_sha256"],
                "source_rows_sha256": receipt["source_rows_sha256"],
                "memory_count": len(FIXTURE), "index_count": len(index_ids),
                "embedding_fingerprint": fingerprint.as_dict(), "row_counts": counts,
                "memory_scheduler_policy": scheduler,
                "configuration_sha256": {name: hashlib.sha256((repository_root / name).read_bytes()).hexdigest()
                    for name in ("characters/miori/miori.card.json", "backend/app/memory/memory_policy.json")}}
    return {"initial_state_hash": digest(evidence), "evidence": evidence}


def inspect_references(data_root: Path, repository_root: Path, conversation_id: str) -> dict[str, object]:
    paths = owned_paths(data_root, repository_root)
    UUID(conversation_id)
    with sqlite3.connect(paths.persona_memory_sqlite_path.as_uri() + "?mode=ro", uri=True) as memory:
        origins = memory.execute("SELECT turn_id FROM memory_response_origins WHERE character_id=? AND conversation_id=?",
                                 ("miori", conversation_id)).fetchall()
        rows = memory.execute("""SELECT d.memory_id,d.content_version FROM memory_response_dependencies d
            JOIN memory_response_origins o ON o.character_id=d.character_id AND o.turn_id=d.turn_id
            WHERE o.character_id=? AND o.conversation_id=? ORDER BY d.memory_id""",
                              ("miori", conversation_id)).fetchall()
    if len(origins) != 1:
        raise ValueError("expected one response provenance origin")
    allowed = {str(value) for value in MEMORY_IDS}
    if any(memory_id not in allowed or version != 1 for memory_id, version in rows):
        raise ValueError("response referenced unexpected memory or version")
    # 未参照も測定結果として残す。成功した参照だけへ試行を差し替えない。
    return {"method": "response_prompt_memory_dependencies_v1", "fixture_version": FIXTURE_VERSION,
            "response_count": len(origins), "referenced_count": len(rows),
            "reference_outcome": "referenced" if rows else "not_referenced",
            "reference_versions_sha256": digest(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "initial", "references"))
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--conversation-id")
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(args.data_root, args.repository_root)
    elif args.action == "initial":
        if args.profile is None or args.conversation_id is None:
            parser.error("initial requires profile and conversation-id")
        result = inspect_initial(args.data_root, args.repository_root, args.profile, args.conversation_id)
    else:
        if args.conversation_id is None:
            parser.error("references requires conversation-id")
        result = inspect_references(args.data_root, args.repository_root, args.conversation_id)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
