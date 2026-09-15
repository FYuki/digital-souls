"""#341の実接続境界受入。通常会話由来の正本を使い、#100候補だけを契約入力する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.conversation_history.config import resolve_conversation_history_config
from app.inference.runtime import create_inference_runtime
from app.memory.chroma_store import (
    active_memory_index_fingerprint,
    list_memory_index_ids,
    query_memories,
)
from app.memory.episodic.read_repository import EpisodicReadRepository
from app.memory.episodic.repository import EpisodicRepository
from app.memory.episodic.sources import (
    ConversationSourceGuard,
    InvalidConversationSource,
)
from app.memory.index_sync import MemoryIndexSync
from app.memory.inference_client import MemoryInferenceEmbedder
from app.memory.memory_policy import resolved_memory_policy
from app.memory.persistence.index_outbox_repository import IndexOutboxRepository
from app.memory.rag_service import retrieve_prompt_memories
from app.memory.semantic.contracts import (
    FormationType,
    Proposition,
    SemanticCandidate,
    SemanticSource,
)
from app.memory.semantic.privacy import SemanticPrivacyReviewer
from app.memory.semantic.read_repository import SemanticReadRepository
from app.memory.semantic.repository import SemanticConflict, SemanticRepository
from app.memory.semantic.service import SemanticRejected, SemanticStore
from app.privacy.scanner import create_privacy_scanner
from app.privacy.semantic.classifier import InferenceSemanticPrivacyClassifier
from app.privacy.semantic.inference_client import InferenceSemanticClassifierClient
from app.runtime_paths import resolve_runtime_paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "phase", choices=("prepare", "invalid", "deleted", "foreign", "priority")
    )
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if root.parent != Path("/tmp") or not root.name.startswith("ds-memory-341-"):
        raise RuntimeError("owned acceptance root required")
    manifest = json.loads((root / "runtime-manifest.json").read_text())
    assert manifest["status"] == "stopped" and manifest["environmentId"] == "test"
    assert (
        manifest["dataRoot"] == str(root / "data")
        and (root / "data").resolve() == root / "data"
    )
    env = {
        **manifest["inferenceSettings"],
        "OLLAMA_BASE_URL": manifest["externalOllama"],
        "DS_ENVIRONMENT_ID": "test",
        "DS_DATA_DIR": manifest["dataRoot"],
    }
    paths = resolve_runtime_paths(env, ROOT)
    config = resolve_conversation_history_config(paths)
    runtime = create_inference_runtime(env)
    clock = lambda: datetime.now(UTC)
    policy = resolved_memory_policy()
    scanner = create_privacy_scanner(policy.privacy)
    client = InferenceSemanticClassifierClient(
        router=runtime.router,
        settings=runtime.settings,
        model_digest_resolver=lambda model, timeout: (
            runtime.ollama_adapter.resolve_model_digest(model, timeout_seconds=timeout)
        ),
    )
    classifier = InferenceSemanticPrivacyClassifier(
        client=client,
        privacy_policy=policy.privacy,
        model_id=client.model_id,
        model_digest_resolver=lambda timeout: client.resolve_model_digest(
            timeout_seconds=timeout
        ),
    )
    guard = ConversationSourceGuard(
        config.database_path, clock=clock, retention=config.retention
    )
    episodes = EpisodicReadRepository(
        EpisodicRepository(paths.persona_memory_sqlite_path), guard
    )
    store = SemanticStore(
        repository=SemanticRepository(paths.persona_memory_sqlite_path),
        source_guard=guard,
        episode_reader=episodes,
        reviewer=SemanticPrivacyReviewer(
            scanner=scanner, classifier=classifier, policy=policy.privacy
        ),
    )
    reader = SemanticReadRepository(store)
    embedder = MemoryInferenceEmbedder(router=runtime.router, settings=runtime.settings)
    sync = MemoryIndexSync(
        approved_repository=reader,
        outbox_repository=IndexOutboxRepository(
            database_path=paths.persona_memory_sqlite_path, clock=clock
        ),
        chroma_path=paths.chroma_path,
        runtime_report_dir=paths.runtime_report_dir,
        embedder=embedder,
        embedding_provider_id=embedder.provider_id,
        embedding_model_id=embedder.model_id,
        clock=clock,
    )

    def indexed():
        return list_memory_index_ids(
            character_id="miori",
            chroma_path=paths.chroma_path,
            fingerprint=active_memory_index_fingerprint("miori", paths.chroma_path),
        )

    observations = []

    class ObservedClassifier:
        def classify(self, text, profile):
            assessment = classifier.classify(text, profile)
            observations.append(assessment)
            return assessment

    observed_classifier = ObservedClassifier()

    def retrieve(query, character="miori"):
        first_observation = len(observations)
        result = retrieve_prompt_memories(
            character,
            query,
            policy,
            scanner=scanner,
            classifier=observed_classifier,
            approved_repository=reader,
            embedder=embedder,
            chroma_path=paths.chroma_path,
            now=clock(),
            timezone="Asia/Tokyo",
        )
        assert (
            len(observations) > first_observation
            and observations[first_observation].classification.value == "NOT_SENSITIVE"
        ), "query gate did not approve; not a retrieval success"
        return [m.memory_id for m in result.memories]

    result = {
        "phase": args.phase,
        "runtime_commit": manifest["commit"],
        "driver_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "run_id": manifest["runId"],
        "models": manifest["models"],
        "environment": "test",
        "generalization_generation": "out_of_scope_contract_input",
    }
    try:
        if args.phase == "prepare":
            events = [
                json.loads(line)
                for line in (root / "lifecycle-events.jsonl").read_text().splitlines()
            ]
            flower_event = next(e for e in events if e["label"] == "source-edit")
            with store.repository.read() as tx:
                flower = next(
                    r
                    for r in tx.list_records("miori")
                    if r.proposition and r.proposition.value == "桜"
                )
            assert str(flower.id) in indexed()
            before = retrieve("私の花の好みは？")
            assert str(flower.id) in before, before
            assert retrieve("私の花の好みは？", "acceptance-other") == []
            # 会話編集APIは未提供。専用の合成発言を編集し、実version triggerを通す故障注入。
            turn_id = flower_event["reply"]["turn"]["turn_id"]
            with sqlite3.connect(paths.sqlite_path) as db:
                previous = db.execute(
                    "SELECT revision FROM memory_turn_versions WHERE turn_id=?",
                    (turn_id,),
                ).fetchone()[0]
                cursor = db.execute(
                    "UPDATE conversation_turns SET user_content=? WHERE turn_id=? AND character_id=?",
                    ("今日は散歩しました。", turn_id, "miori"),
                )
                assert cursor.rowcount == 1
                current = db.execute(
                    "SELECT revision FROM memory_turn_versions WHERE turn_id=?",
                    (turn_id,),
                ).fetchone()[0]
                assert current > previous
            # index workerのない停止中rootで、古い実ベクトルの存在と検索候補からの排除を別々に確認。
            raw = query_memories(
                "miori",
                embedder("私の花の好みは？"),
                n_results=50,
                chroma_path=paths.chroma_path,
                fingerprint=active_memory_index_fingerprint("miori", paths.chroma_path),
            )
            assert str(flower.id) in {r.memory_id for r in raw}
            after = retrieve("私の花の好みは？")
            assert str(flower.id) not in after
            assert str(flower.id) in indexed()
            stale = reader.get(character_id="miori", memory_id=flower.id)
            assert stale.status.value == "INACTIVE"
            result["source_edit"] = {
                "record_id": str(flower.id),
                "revision_before": previous,
                "revision_after": current,
                "raw_chroma_contains_old_id": True,
                "retrieval_excludes_old_id": True,
                "status": "INACTIVE",
                "foreign_results": [],
            }
            roots = []
            for label in ("derived-source", "derived-source-two"):
                event = next(e for e in events if e["label"] == label)
                with episodes.repository.read() as tx:
                    candidates = tx.list_records("miori")
                    valid = [
                        r
                        for r in candidates
                        if r.kind.value == "EPISODE"
                        and str(r.conversation_id) == event["conversation_id"]
                        and episodes.get(
                            character_id="miori", memory_id=r.id
                        ).status.value
                        == "ACTIVE"
                    ]
                assert valid, ("missing real Episode", label)
                roots.append(
                    SemanticSource(
                        kind="EPISODE",
                        source_id=valid[0].id,
                        revision=valid[0].content_version,
                    )
                )

            def candidate(predicate, value):
                return SemanticCandidate(
                    formation_type=FormationType.EXPERIENCE_DERIVED,
                    proposition=Proposition(
                        subject="ユーザー",
                        predicate=predicate,
                        value=value,
                        content=f"ユーザーの{predicate}は{value}",
                        mutability="CHANGEABLE",
                        self_report=False,
                    ),
                    sources=tuple(roots),
                    confidence=0.8,
                )

            first = candidate("コーヒーの好み", "好き")
            # #100から届く形式を明示した候補。一般化をLLMが生成したという証明にはしない。
            saved = store.save(
                character_id="miori",
                candidate=first,
                receipt_key="acceptance-derived-delete",
            )
            again = store.save(
                character_id="miori",
                candidate=first,
                receipt_key="acceptance-derived-delete",
            )
            assert (
                saved.id == again.id and saved.content_version == again.content_version
            )
            invalidation = store.save(
                character_id="miori",
                candidate=candidate("朝の飲み物の傾向", "コーヒー"),
                receipt_key="acceptance-derived-invalidate",
            )
            for bad_character, bad in [
                ("acceptance-other", first),
                (
                    "miori",
                    first.model_copy(
                        update={
                            "sources": (
                                roots[0].model_copy(
                                    update={"revision": roots[0].revision + 1}
                                ),
                                roots[1],
                            )
                        }
                    ),
                ),
            ]:
                try:
                    store.save(
                        character_id=bad_character,
                        candidate=bad,
                        receipt_key=str(uuid4()),
                    )
                except (SemanticConflict, InvalidConversationSource):
                    pass
                else:
                    raise AssertionError("invalid provenance accepted")
            sync.run_worker_once()
            assert {str(saved.id), str(invalidation.id)} <= indexed()
            found = retrieve("私のコーヒーの好みは？")
            assert str(saved.id) in found, found
            assert retrieve("私のコーヒーの好みは？", "acceptance-other") == []
            result["derived"] = {
                "delete_id": str(saved.id),
                "invalidate_id": str(invalidation.id),
                "sources": [s.model_dump(mode="json") for s in roots],
                "candidate": first.model_dump(mode="json"),
                "real_privacy_stamp": saved.stamp.model_dump(mode="json"),
                "retrieved_ids": found,
                "duplicate_receipt_is_same_record": True,
                "foreign_and_stale_source_rejected": True,
            }
        elif args.phase == "deleted":
            prepared = json.loads((root / "boundaries-prepare.json").read_text())[
                "derived"
            ]
            target = UUID(prepared["delete_id"])
            with store.repository.read() as tx:
                record = tx.get("miori", target)
                assert record.status.value == "DELETED" and record.proposition is None
                assert all(
                    row[0] is None
                    for row in tx.connection.execute(
                        "SELECT proposition FROM semantic_versions WHERE record_id=?",
                        (str(target),),
                    )
                )
            assert str(target) not in indexed()
            try:
                store.save(
                    character_id="miori",
                    candidate=SemanticCandidate.model_validate(prepared["candidate"]),
                    receipt_key=str(uuid4()),
                )
            except SemanticRejected:
                pass
            else:
                raise AssertionError("deleted Episode evidence regenerated")
            for s in prepared["sources"]:
                assert (
                    episodes.get(
                        character_id="miori", memory_id=UUID(s["source_id"])
                    ).status.value
                    == "ACTIVE"
                )
            result.update(
                record_id=str(target),
                all_version_bodies_erased=True,
                source_episodes_preserved=True,
                removed_from_real_chroma=True,
                same_evidence_regeneration_rejected=True,
            )
        elif args.phase == "priority":
            prepared = json.loads((root / "boundaries-prepare.json").read_text())[
                "derived"
            ]
            event = next(
                json.loads(line)
                for line in (root / "lifecycle-events.jsonl").read_text().splitlines()
                if json.loads(line)["label"] == "derived-new-source"
            )
            with episodes.repository.read() as tx:
                fresh_episode = next(
                    r
                    for r in tx.list_records("miori")
                    if r.kind.value == "EPISODE"
                    and str(r.conversation_id) == event["conversation_id"]
                )
            old = SemanticCandidate.model_validate(prepared["candidate"])
            fresh = old.model_copy(
                update={
                    "sources": (
                        old.sources[1],
                        SemanticSource(
                            kind="EPISODE",
                            source_id=fresh_episode.id,
                            revision=fresh_episode.content_version,
                        ),
                    )
                }
            )
            saved = store.save(
                character_id="miori",
                candidate=fresh,
                receipt_key="acceptance-derived-new-source",
            )
            with store.repository.read() as tx:
                self_report = next(
                    r
                    for r in tx.list_records("miori")
                    if r.proposition
                    and r.proposition.self_report
                    and r.proposition.predicate == "コーヒーの好み"
                    and r.status.value == "ACTIVE"
                )
                assert tx.get("miori", saved.id).status.value == "ACTIVE"
                assert self_report.proposition.value == "好きではない"
            assert (
                reader.get(character_id="miori", memory_id=saved.id).status.value
                == "INACTIVE"
            )
            assert str(self_report.id) in retrieve("私のコーヒーの好みは？")
            result.update(
                derived_id=str(saved.id),
                self_report_id=str(self_report.id),
                canonical_both_active=True,
                derived_excluded_from_retrieval=True,
                new_episode_source_after_deletion_accepted=True,
            )
        elif args.phase == "foreign":
            from app.memory.chroma_store import (
                activate_memory_index,
                upsert_memory_index_entry,
            )

            with store.repository.read() as tx:
                current = next(
                    r
                    for r in tx.list_records("miori")
                    if r.proposition
                    and r.proposition.self_report
                    and r.status.value == "ACTIVE"
                )
            fingerprint = active_memory_index_fingerprint("miori", paths.chroma_path)
            foreign = "acceptance-other"
            # 検証専用の別namespaceへ誤ったIDを注入。SQLite所有検査で実候補を排除する。
            activate_memory_index(foreign, fingerprint, paths.chroma_path)
            upsert_memory_index_entry(
                character_id=foreign,
                memory_id=str(current.id),
                embedding=embedder(current.proposition.content),
                normalized_text=current.proposition.content,
                provider_id="core",
                memory_kind="SEMANTIC",
                memory_type="DIRECT_EXTRACTION",
                policy_version=policy.policy_version,
                occurred_at=None,
                expires_at=None,
                chroma_path=paths.chroma_path,
                fingerprint=fingerprint,
            )
            raw = query_memories(
                foreign,
                embedder("私のコーヒーの好みは？"),
                n_results=10,
                chroma_path=paths.chroma_path,
                fingerprint=fingerprint,
            )
            assert str(current.id) in {item.memory_id for item in raw}
            assert retrieve("私のコーヒーの好みは？", foreign) == []
            assert retrieve("私のコーヒーの好みは？")
            result.update(
                foreign_namespace_contains_target=True,
                rejected_by_canonical_owner=True,
                record_id=str(current.id),
                selected_foreign_ids=[],
            )
        else:
            prepared = json.loads((root / "boundaries-prepare.json").read_text())[
                "derived"
            ]
            target = UUID(prepared["invalidate_id"])
            assert str(target) in indexed()
            episode_id = prepared["sources"][0]["source_id"]
            # #100未実装のため、根拠Episodeの失効イベントを専用DBだけに注入する。
            with sqlite3.connect(paths.persona_memory_sqlite_path) as db:
                assert (
                    db.execute(
                        "UPDATE episodic_records SET status='INACTIVE' WHERE id=? AND character_id='miori'",
                        (episode_id,),
                    ).rowcount
                    == 1
                )
            raw = query_memories(
                "miori",
                embedder("私の朝の飲み物の傾向は？"),
                n_results=50,
                chroma_path=paths.chroma_path,
                fingerprint=active_memory_index_fingerprint("miori", paths.chroma_path),
            )
            assert str(target) in {r.memory_id for r in raw}
            assert str(target) not in retrieve("私の朝の飲み物の傾向は？")
            with store.repository.read() as tx:
                assert tx.get("miori", target).status.value == "INACTIVE"
                row = tx.connection.execute(
                    "SELECT * FROM semantic_reassessment WHERE record_id=? AND completed_at IS NULL",
                    (str(target),),
                ).fetchone()
                assert row and row["formation_type"] == "EXPERIENCE_DERIVED"
            assert str(target) in indexed()
            sync.run_worker_once()
            assert str(target) not in indexed()
            result.update(
                record_id=str(target),
                episode_id=episode_id,
                raw_stale_index_rejected=True,
                reassessment_pending=True,
                eventually_removed_from_chroma=True,
            )
        (root / f"boundaries-{args.phase}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "passed": True,
                    "artifact": str(root / f"boundaries-{args.phase}.json"),
                }
            ),
            flush=True,
        )
    except Exception as error:
        result.update(passed=False, error_class=type(error).__name__)
        failure = root / f"boundaries-{args.phase}-failed-{uuid4()}.json"
        failure.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        raise
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
