"""promptfooから本番と同じ抽出・privacy・保存経路を呼ぶ。正解は推論へ渡さない。"""

import atexit
from dataclasses import asdict
from functools import lru_cache
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.request import urlopen

from app.inference import InferenceCaller, InferenceTarget
from app.inference.router import InferenceRouter
from app.inference.runtime import create_inference_runtime
from app.memory.episodic.contracts import ExtractionIdentity
from app.memory.inference_client import StructuredMemoryInferenceClient
from app.memory.memory_policy import resolved_memory_policy
from app.memory.semantic.extractor import PROMPT_VERSION, SemanticExtractor
from app.memory.semantic.pipeline import SemanticPipeline
from app.memory.semantic.privacy import SemanticPrivacyReviewer
from app.memory.semantic.worker import input_batches
from app.privacy.scanner import create_privacy_scanner
from app.privacy.semantic.classifier import InferenceSemanticPrivacyClassifier
from app.privacy.semantic.inference_client import InferenceSemanticClassifierClient
from evals.semantic_memory.fixtures import Fixture


class ObservedPrivacy:
    def __init__(self, classifier, output):
        self.classifier, self.output = classifier, output

    def classify(self, text, profile):
        result = self.classifier.classify(text, profile)
        self.output.setdefault("privacy_assessments", []).append(asdict(result))
        return result


@lru_cache(maxsize=1)
def runtime():
    if os.environ.get("PRIVACY_EVAL_STUB"):
        raise ValueError("real semantic evaluation forbids privacy stubs")
    value = create_inference_runtime(os.environ)
    target = value.settings.target(InferenceTarget.SEMANTIC_EXTRACTION)
    if target.reference.provider_id != "ollama" or target.reference.model_id not in {"gemma4:e4b", "gemma4:12b"}:
        value.close()
        raise ValueError("semantic comparison requires local Gemma 4 e4b or 12b")
    atexit.register(value.close)
    return value


def call_api(prompt, options, context):
    del options
    started = time.monotonic()
    output = {"case_id": context["vars"]["case_id"], "prompt_version": PROMPT_VERSION,
              "saved": [], "proposed": [], "observations": []}
    fixture = None
    with tempfile.TemporaryDirectory(prefix="ds341-semantic-eval-") as folder:
        try:
            data = json.loads(prompt)
            if set(data) - {"turns", "existing"}:
                raise ValueError("evaluation prompt contains non-input fields")
            inference = runtime()
            target = inference.settings.target(InferenceTarget.SEMANTIC_EXTRACTION)
            reference = target.reference
            digest = inference.ollama_adapter.resolve_model_digest(
                reference.model_id, timeout_seconds=30, refresh=True,
            )
            identity = ExtractionIdentity(provider_id=reference.provider_id, model_id=reference.model_id,
                                          model_digest=digest, prompt_version=PROMPT_VERSION)
            output.update({"model_id": reference.model_id, "model_digest": digest,
                           "options": dict(target.options), "max_input_tokens": target.max_input_tokens,
                           "max_output_tokens": target.max_output_tokens, "timeout_seconds": target.timeout_seconds})
            router = InferenceRouter(settings=inference.settings, registry=inference.registry,
                observer=lambda observation: output["observations"].append(asdict(observation)))
            policy = resolved_memory_policy()
            privacy_client = InferenceSemanticClassifierClient(
                router=router, settings=inference.settings,
                model_digest_resolver=lambda model_id, timeout_seconds:
                    inference.ollama_adapter.resolve_model_digest(model_id, timeout_seconds=timeout_seconds),
            )
            privacy = InferenceSemanticPrivacyClassifier(client=privacy_client, privacy_policy=policy.privacy,
                model_id=inference.settings.target(InferenceTarget.PRIVACY).reference.model_id,
                model_digest_resolver=lambda timeout_seconds:
                    privacy_client.resolve_model_digest(timeout_seconds=timeout_seconds))
            fixture = Fixture(Path(folder), SemanticPrivacyReviewer(
                scanner=create_privacy_scanner(policy.privacy), classifier=ObservedPrivacy(privacy, output), policy=policy.privacy))
            pending = fixture.prepare(data)
            output["policy_version"] = policy.privacy.policy_version
            client = StructuredMemoryInferenceClient(router=router, settings=inference.settings,
                caller=InferenceCaller.SEMANTIC_EXTRACTION, target=InferenceTarget.SEMANTIC_EXTRACTION)
            pipeline = SemanticPipeline(fixture.store, SemanticExtractor(client,
                timeout_seconds=target.timeout_seconds, max_output_tokens=target.max_output_tokens),
                timezone="Asia/Tokyo")
            turn = pending.primary.turn
            result = pipeline.process(character_id="miori", conversation_id=turn.conversation_id,
                source_id=turn.turn_id, revision=pending.primary.revision, batches=input_batches(pending),
                catalog=fixture.store.reconcile("miori"), extraction=identity)
            keys = {r.id: key for key, r in fixture.known.items()}
            output["proposed"] = [{
                "operation": p.operation.value, "target": keys.get(p.target.id) if p.target else None,
                "proposition": p.candidate.proposition.model_dump(mode="json"),
            } for p in result.proposed]
            output["saved"] = [record.model_dump(mode="json") for record in result.saved]
            with fixture.repository.read() as tx:
                relations = tx.relations("miori")
            output["saved_operations"] = []
            for record in result.saved:
                relation = next((r for r in relations if r.target_id == record.id), None)
                operation = ("REAFFIRM" if record.id in keys else
                             relation.relation.value if relation else "NEW")
                target_key = keys.get(record.id) if operation == "REAFFIRM" else (
                    keys.get(relation.source_id) if relation else None)
                output["saved_operations"].append({"operation": operation, "target": target_key})
            output["rejected"] = result.rejected
            output["source_turns"] = [[fixture.turn_indexes.get(str(s.source_id)) for s in r.sources
                                       if s.span and s.span.role == "user"] for r in result.saved]
        except Exception as error:
            output["error_type"] = type(error).__name__
        finally:
            if fixture is not None:
                with fixture.repository.read() as tx:
                    output["existing_status"] = {key: tx.get(r.character_id, r.id).status.value
                                                  for key, r in fixture.known.items()}
                    original = {r.id: r.content_version for r in fixture.known.values()}
                    changed = [r for r in tx.list_records("miori") if original.get(r.id) != r.content_version]
                    output["committed"] = [r.model_dump(mode="json") for r in changed]
                    foreign = [r for r in tx.list_records("other-character")
                               if original.get(r.id) != r.content_version]
                    output["foreign_modified"] = bool(foreign)
            try:
                endpoint = os.environ["OLLAMA_BASE_URL"].rstrip("/")
                with urlopen(endpoint + "/api/ps", timeout=5) as response:
                    output["loaded_models"] = json.load(response)["models"]
            except Exception:
                output["loaded_models"] = None
    output["latency_seconds"] = time.monotonic() - started
    progress = os.environ.get("SEMANTIC_EVAL_PROGRESS")
    if progress:
        with Path(progress).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(output, ensure_ascii=False, default=str) + "\n")
    return {"output": json.dumps(output, ensure_ascii=False, default=str)}
