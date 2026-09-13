"""本番のFact 5W再検証だけを実LLMで評価する。DB・privacy保存判定は対象外。"""

import atexit
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import UUID

from app.inference import InferenceCaller, InferenceTarget
from app.inference.runtime import create_inference_runtime
from app.memory.episodic.contracts import ExtractedFiveW, What
from app.memory.episodic.extraction_contracts import ExtractionBatch, ExtractedRecord, SourceQuote
from app.memory.formation.config import resolve_memory_formation_settings
from app.memory.formation.episodic_extractor import EPISODIC_EXTRACTOR_VERSION, ThreadEpisodeExtractor
from app.memory.inference_client import StructuredMemoryInferenceClient

LABELS = {"speaker:user": "ユーザー", "character:miori": "光織"}


@lru_cache(maxsize=1)
def runtime():
    value = create_inference_runtime(os.environ)
    target = value.settings.target(InferenceTarget.MEMORY_EXTRACTION)
    if target.reference.provider_id != "ollama":
        value.close()
        raise ValueError("actor evaluation requires the configured local Ollama target")
    atexit.register(value.close)
    return value


def build_request(prompt):
    # expected_jsonや正解人物はここへ渡さない。対象行為だけを固定して人物判断を切り出す。
    data = json.loads(prompt)
    participants = {
        "user": {"entity_id": "speaker:user", "name": LABELS["speaker:user"]},
        "assistant": {"entity_id": "character:miori", "name": LABELS["character:miori"]},
    }
    fragments = []
    for index, turn in enumerate(data["turns"]):
        role, text = turn["role"], turn["text"]
        source_id = UUID(bytes=hashlib.sha256(f"{prompt}:{index}".encode()).digest()[:16], version=4)
        fragments.append({
            "source_id": str(source_id), "revision": 2, "role": role,
            "speaker": participants[role],
            "addressee": participants["assistant" if role == "user" else "user"],
            "start": 0, "end": len(text), "ownership": "primary", "text": text,
            "processed_ranges": [],
        })
    part = fragments[data["anchor_index"]]
    quote = SourceQuote(source_id=part["source_id"], revision=2, role=part["role"],
                        quote=part["text"], start=0)
    candidate = ExtractedRecord(
        key="fact", kind="FACT", operation="NEW",
        five_w=ExtractedFiveW(what=What(**data["target"])), anchor=quote, sources=(quote,),
    )
    payload = {"character_id": "miori", "conversation_id": fragments[0]["source_id"],
               "fragments": fragments, "known_records": [], "entity_labels": LABELS}
    return ExtractionBatch(records=(candidate,)), payload


def call_api(prompt, options, context):
    del options
    case_id = context["vars"]["case_id"]
    started = time.monotonic()
    output = {"case_id": case_id, "prompt_version": EPISODIC_EXTRACTOR_VERSION}
    try:
        inference = runtime()
        target = inference.settings.target(InferenceTarget.MEMORY_EXTRACTION)
        reference = target.reference
        output.update({
            "model_id": reference.model_id,
            "input_token_limit": target.max_input_tokens,
            "output_token_limit": target.max_output_tokens,
            "temperature": target.options.get("temperature"), "think": target.options.get("think"),
            "model_digest": inference.ollama_adapter.resolve_model_digest(
                reference.model_id, timeout_seconds=30),
        })
        client = StructuredMemoryInferenceClient(
            router=inference.router, caller=InferenceCaller.MEMORY_EXTRACTION,
            target=InferenceTarget.MEMORY_EXTRACTION, settings=inference.settings,
        )
        extractor_class = ThreadEpisodeExtractor
        if os.environ.get("EPISODIC_EVAL_DESIGN") != "legacy":
            from evals.episodic_quality.compact import CompactExtractor, design_fingerprint
            extractor_class = CompactExtractor
            from app.memory.formation.compact_extractor import COMPACT_EXTRACTOR_VERSION
            output["prompt_version"] = COMPACT_EXTRACTOR_VERSION + "-" + design_fingerprint()[:12]
        extractor = extractor_class(client=client, settings=resolve_memory_formation_settings(os.environ))
        batch, payload = build_request(prompt)
        result = extractor._ground_content(batch, payload, [], LABELS, lambda: False)
        output["who"] = [person.model_dump(mode="json") for person in result.records[0].five_w.who]
    except Exception as error:
        # 推論失敗も採点対象に残す。例外本文・promptは標準ログへ出さない。
        output["error_type"] = type(error).__name__
    output["latency_seconds"] = time.monotonic() - started
    progress = os.environ.get("EPISODIC_ACTOR_EVAL_PROGRESS")
    if progress:
        with Path(progress).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(output, ensure_ascii=False) + "\n")
    return {"output": json.dumps(output, ensure_ascii=False)}
