"""アプリの設定・推論・privacy境界から永続Episode形成を構成する。"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path

from app.inference import InferenceTarget
from app.inference.runtime import InferenceRuntime
from app.memory.episodic.contracts import ExtractionIdentity
from app.memory.episodic.privacy import EpisodicPrivacyReviewer
from app.memory.episodic.registration import EpisodicRegistrationService
from app.memory.episodic.repository import EpisodicRepository
from app.memory.formation.config import MemoryFormationSettings
from app.memory.formation.durable_scheduler import DurableMemoryFormationScheduler
from app.memory.formation.compact_extractor import COMPACT_EXTRACTOR_VERSION, CompactExtractor
from app.memory.formation.episodic_worker import EpisodicFormationWorker
from app.memory.formation.thread_queue import ThreadFormationQueue
from app.memory.inference_client import StructuredMemoryInferenceClient


def extraction_identity(
    runtime: InferenceRuntime, *, timeout_seconds: float,
) -> ExtractionIdentity:
    reference = runtime.settings.target(InferenceTarget.MEMORY_EXTRACTION).reference
    if reference.provider_id != "ollama":
        # 他Providerのモデル識別情報をOllamaの同名モデルのdigestで代用しない。
        raise ValueError("episodic extraction requires a provider with verifiable model identity")
    return ExtractionIdentity(
        provider_id=reference.provider_id, model_id=reference.model_id,
        model_digest=runtime.ollama_adapter.resolve_model_digest(
            reference.model_id, timeout_seconds=timeout_seconds, refresh=True,
        ),
        prompt_version=COMPACT_EXTRACTOR_VERSION,
    )


def build_episodic_scheduler(
    *, history_path: Path, repository: EpisodicRepository,
    clock: Callable[[], datetime], retention: timedelta, timezone: str,
    reviewer: EpisodicPrivacyReviewer, client: StructuredMemoryInferenceClient,
    settings: MemoryFormationSettings, runtime: InferenceRuntime,
    entity_labels: Callable[[str], Mapping[str, str]],
) -> DurableMemoryFormationScheduler:
    queue = ThreadFormationQueue(database_path=history_path, clock=clock, retention=retention)
    registration = EpisodicRegistrationService(
        repository=repository, queue=queue, reviewer=reviewer, timezone=timezone, clock=clock,
    )
    return DurableMemoryFormationScheduler(
        queue=queue,
        worker=EpisodicFormationWorker(
            queue=queue, extractor=CompactExtractor(client=client, settings=settings),
            registration=registration, entity_labels=entity_labels,
            extraction_identity=lambda: extraction_identity(
                runtime, timeout_seconds=settings.llm_timeout_seconds,
            ),
        ),
    )
