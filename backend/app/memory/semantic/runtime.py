"""意味記憶専用Targetを既存の推論ルーターと永続ワーカーへ接続する。"""

from app.inference import InferenceCaller, InferenceTarget
from app.inference.runtime import InferenceRuntime
from app.memory.episodic.contracts import ExtractionIdentity
from app.memory.formation.durable_scheduler import DurableMemoryFormationScheduler
from app.memory.inference_client import StructuredMemoryInferenceClient
from app.memory.semantic.extractor import PROMPT_VERSION, SemanticExtractor
from app.memory.semantic.pipeline import SemanticPipeline
from app.memory.semantic.service import SemanticStore
from app.memory.semantic.worker import SemanticWorkQueue, SemanticWorker, conversation_idle


def build_semantic_scheduler(
    *, store: SemanticStore, runtime: InferenceRuntime, timezone: str,
) -> DurableMemoryFormationScheduler:
    target = runtime.settings.target(InferenceTarget.SEMANTIC_EXTRACTION)
    assert target.max_output_tokens is not None
    client = StructuredMemoryInferenceClient(
        router=runtime.router, settings=runtime.settings,
        caller=InferenceCaller.SEMANTIC_EXTRACTION, target=InferenceTarget.SEMANTIC_EXTRACTION,
    )
    queue = SemanticWorkQueue(store)

    def identity() -> ExtractionIdentity:
        reference = target.reference
        # cloud等で実体digestが取得できない場合は、その不在を明示する。
        # Ollamaの別モデルのdigestや自作hashで代用しない。
        digest = "unavailable:provider-does-not-expose-model-digest"
        if reference.provider_id == "ollama":
            digest = runtime.ollama_adapter.resolve_model_digest(
                reference.model_id, timeout_seconds=target.timeout_seconds, refresh=True,
            )
        return ExtractionIdentity(provider_id=reference.provider_id, model_id=reference.model_id,
                                  model_digest=digest, prompt_version=PROMPT_VERSION)

    def priority_available() -> bool:
        with store.source_guard.snapshot() as (history, _):
            return conversation_idle(history, now=store.clock())

    return DurableMemoryFormationScheduler(queue=queue, worker=SemanticWorker(
        queue=queue, identity=identity, priority_available=priority_available,
        pipeline=SemanticPipeline(store, SemanticExtractor(
            client, timeout_seconds=target.timeout_seconds, max_output_tokens=target.max_output_tokens,
        ), timezone=timezone),
    ))
