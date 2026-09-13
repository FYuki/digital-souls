"""現行の抽出・内容確認・catalog照合・privacyを実LLMで個別評価する。"""
import json
import os
from pathlib import Path
import time
from datetime import datetime, UTC
from uuid import UUID

from app.conversation_history.models import ConversationTurn, TurnStatus
from app.memory.episodic.quotes import validate_record_anchors
from app.memory.formation.thread_queue import ThreadLease, ThreadSnapshot, ThreadSource
from app.memory.formation.thread_chunks import ThreadChunk, ThreadFragment

from app.inference import InferenceCaller, InferenceTarget
from app.memory.episodic.extraction_contracts import ExtractionBatch
from app.memory.formation.catalog_scan import CATALOG_SCAN_PROMPT, CatalogMatches
from app.memory.formation.config import resolve_memory_formation_settings
from app.memory.formation.episodic_extractor import (
    EPISODIC_EXTRACTOR_VERSION, SYSTEM_PROMPT, ThreadEpisodeExtractor,
)
from app.memory.inference_client import StructuredMemoryInferenceClient
from evals.episodic_actor.provider import LABELS, build_request, runtime


def build_input(prompt):
    """正解は受け取らず、合成会話・既存記録・対象行為だけを入力する。"""
    data = json.loads(prompt)
    batch, payload = build_request(json.dumps({
        "turns": data["turns"], "anchor_index": data.get("anchor_index", 0),
        "target": {k: v for k, v in data.get("target", {"predicate": "した"}).items()
                   if k in {"predicate", "object"}},
    }, ensure_ascii=False))
    for index, part in enumerate(payload["fragments"]):
        part["ownership"] = "context_before" if index < data.get("primary_from", 0) else "primary"
    payload["known_records"] = data.get("known", [])
    if payload["known_records"]:
        payload["conversation_id"] = payload["known_records"][0]["conversation_id"]
    return data, batch, payload


def anchor_validator(payload):
    """本番と同じanchor検証・修復を行うため、提示断片に対応する一時snapshotを作る。"""
    now = datetime(2026, 9, 13, 0, 0, tzinfo=UTC)
    conversation = UUID(payload["conversation_id"])
    sources, primary, before = [], [], []
    for fragment in payload["fragments"]:
        role = fragment["role"]
        turn = ConversationTurn(
            turn_id=UUID(fragment["source_id"]), character_id="miori",
            conversation_id=conversation,
            user_content=fragment["text"] if role == "user" else "",
            assistant_content=fragment["text"] if role == "assistant" else "",
            status=TurnStatus.COMPLETED, privacy_reason_code=None,
            created_at=now, updated_at=now,
        )
        source = ThreadSource(turn, fragment["revision"])
        sources.append(source)
        part = ThreadFragment(source, role, 0, len(fragment["text"]))
        (primary if fragment["ownership"] == "primary" else before).append(part)
    snapshot = ThreadSnapshot(
        ThreadLease("miori", conversation, 2, conversation), tuple(sources), now)
    chunk = ThreadChunk(0, tuple(primary), tuple(before), ())
    def validate(batch):
        if batch.complete:
            validate_record_anchors(batch.records, snapshot, chunk)
    return validate


def call_api(prompt, options, context):
    del options
    started = time.monotonic()
    output = {"case_id": context["vars"]["case_id"]}
    try:
        data = json.loads(prompt)
        output["stage"] = data["stage"]
        if data["stage"] == "privacy":
            # 既存のproduction providerを再利用し、ADMISSIONの予算・再試行を保持する。
            if os.environ.get("PRIVACY_EVAL_STUB") == "1":
                raise ValueError("real evaluation cannot use a stub")
            from evals.privacy_classifier.provider import call_api as privacy_call
            output.update(json.loads(privacy_call(
                data["text"], {"config": {"profile": "ADMISSION"}}, context,
            )["output"]))
        else:
            inference = runtime()
            target = inference.settings.target(InferenceTarget.MEMORY_EXTRACTION)
            output.update({
                "model_id": target.reference.model_id,
                "model_digest": inference.ollama_adapter.resolve_model_digest(
                    target.reference.model_id, timeout_seconds=30),
                "prompt_version": EPISODIC_EXTRACTOR_VERSION,
            })
            client = StructuredMemoryInferenceClient(
                router=inference.router, caller=InferenceCaller.MEMORY_EXTRACTION,
                target=InferenceTarget.MEMORY_EXTRACTION, settings=inference.settings,
            )
            extractor = ThreadEpisodeExtractor(
                client=client, settings=resolve_memory_formation_settings(os.environ))
            data, batch, payload = build_input(prompt)
            if data["stage"] == "ground":
                result = extractor._ground_content(
                    batch, payload, payload["known_records"], LABELS, lambda: False)
                output["batch"] = result.model_dump(mode="json")
            elif data["stage"] == "catalog":
                payload.update({"phase": "catalog_match",
                                "candidates": [r.model_dump(mode="json") for r in batch.records]})
                result = extractor._infer(
                    extractor._messages(CATALOG_SCAN_PROMPT, payload), CatalogMatches, lambda: False)
                output["catalog"] = result.model_dump(mode="json")
            elif data["stage"] == "extract":
                # 本番と同じく操作選定後に5Wを再検証し、確定した抽出結果を測る。
                result = extractor._infer(
                    extractor._messages(SYSTEM_PROMPT, payload), ExtractionBatch, lambda: False,
                    anchor_validator(payload))
                if result.complete:
                    result = extractor._ground_content(
                        result, payload, payload["known_records"], LABELS, lambda: False)
                output["batch"] = result.model_dump(mode="json")
            else:
                raise ValueError("unknown evaluation stage")
    except Exception as error:
        output["error_type"] = type(error).__name__
    output["latency_seconds"] = time.monotonic() - started
    progress = os.environ.get("EPISODIC_QUALITY_EVAL_PROGRESS")
    if progress:
        with Path(progress).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(output, ensure_ascii=False) + "\n")
    return {"output": json.dumps(output, ensure_ascii=False)}
