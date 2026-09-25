"""記憶のRepository・管理service・推論client・schedulerを所有する。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.inference import InferenceCaller, InferenceTarget
from app.inference.runtime import target_model_id
from app.memory.admission.evaluator import create_rag_admission_evaluator
from app.memory.admission_service import RagAdmissionService
from app.memory.consolidation.config import (
    MemoryConsolidationSettings,
    resolve_memory_consolidation_settings,
)
from app.memory.consolidation.planner import ConsolidationPlanner
from app.memory.consolidation.privacy import ConsolidationPrivacyReviewer
from app.memory.consolidation.scheduler import (
    ConsolidationPriorityProbe,
    MemoryConsolidationScheduler,
    is_consolidation_eligible,
)
from app.memory.consolidation.service import MemoryConsolidationService
from app.memory.episodic.management import EpisodicMemoryManagement
from app.memory.episodic.privacy import EpisodicPrivacyReviewer
from app.memory.episodic.read_repository import (
    CombinedMemoryReadRepository,
    EpisodicReadRepository,
)
from app.memory.episodic.repository import EpisodicRepository
from app.memory.episodic.sources import ConversationSourceGuard
from app.memory.formation.combined_scheduler import (
    CombinedFormationScheduler,
    FormationScheduler,
)
from app.memory.formation.config import (
    MemoryFormationSettings,
    resolve_memory_formation_settings,
)
from app.memory.formation.extractor import EXTRACTOR_VERSION, MemoryCandidateExtractor
from app.memory.formation.runtime import build_episodic_scheduler
from app.memory.formation.scheduler import MemoryFormationScheduler
from app.memory.formation.worker import MemoryFormationWorker
from app.memory.index_scheduler import MemoryIndexScheduler
from app.memory.index_sync import MemoryIndexSync
from app.memory.inference_client import (
    MemoryInferenceEmbedder,
    StructuredMemoryInferenceClient,
)
from app.memory.persistence.approved_repository import ApprovedMemoryRepository
from app.memory.persistence.index_outbox_repository import IndexOutboxRepository
from app.memory.persistence.temporary_repository import (
    TemporaryProviderRecordRepository,
)
from app.memory.providers import AddonRecordProvider, PersonaMemoryProvider
from app.memory.semantic.management import SemanticMemoryManagement
from app.memory.semantic.privacy import SemanticPrivacyReviewer
from app.memory.semantic.read_repository import (
    SemanticReadRepository,
    WithSemanticReadRepository,
)
from app.memory.semantic.repository import SemanticRepository
from app.memory.semantic.runtime import build_semantic_scheduler
from app.memory.semantic.service import SemanticStore
from app.privacy.semantic.classifier import InferenceSemanticPrivacyClassifier
from app.privacy.semantic.inference_client import InferenceSemanticClassifierClient
from app.voice_measurement_memory import DisabledFormationScheduler

if TYPE_CHECKING:
    from fastapi import FastAPI
    from app.inference.runtime import InferenceRuntime
    from app.memory.response_provenance_recorder import ResponseProvenanceRecorder
    from app.runtime.history import HistoryResources
    from app.runtime.inference import InferenceResources
    from app.runtime.privacy import PrivacyResources
    from app.runtime_paths import RuntimePaths

CONSOLIDATION_PROMPT_VERSION = "consolidation-v1"


class MemoryResources:
    """記憶ドメインの永続化・管理・推論・schedulerを所有する資源owner。"""

    def __init__(self) -> None:
        self.approved_repository: ApprovedMemoryRepository | None = None
        self.episodic_repository: EpisodicRepository | None = None
        self.legacy_read_repository: CombinedMemoryReadRepository | None = None
        self.read_repository: WithSemanticReadRepository | None = None
        self.provenance_recorder: ResponseProvenanceRecorder | None = None
        self.outbox_repository: IndexOutboxRepository | None = None
        self.embedder: MemoryInferenceEmbedder | None = None
        self.index_sync: MemoryIndexSync | None = None
        self.temporary_record_repository: (
            TemporaryProviderRecordRepository | None
        ) = None
        self.index_scheduler: MemoryIndexScheduler | None = None
        self.formation_settings: MemoryFormationSettings | None = None
        self.consolidation_settings: MemoryConsolidationSettings | None = None
        self.semantic_store: SemanticStore | None = None
        self.rag_admission_service: RagAdmissionService | None = None
        self.extractor_client: StructuredMemoryInferenceClient | None = None
        self.consolidation_client: StructuredMemoryInferenceClient | None = None
        self.consolidation_classifier_client: (
            InferenceSemanticClassifierClient | None
        ) = None
        self.consolidation_privacy_classifier: (
            InferenceSemanticPrivacyClassifier | None
        ) = None
        self.candidate_extractor: MemoryCandidateExtractor | None = None
        self.preference_scheduler: MemoryFormationScheduler | None = None
        self.formation_scheduler: FormationScheduler | None = None
        self.consolidation_scheduler: MemoryConsolidationScheduler | None = None
        self.clock: Callable[[], datetime] | None = None
        self.runtime_paths: RuntimePaths | None = None
        self.semantic_published = False
        self.persona_published = False
        self.episodic_published = False
        self.addon_published = False
        self.rag_published = False
        self.formation_started = False
        self.consolidation_started = False
        self.index_started = False

    def initialize_schema(
        self, runtime_paths: RuntimePaths, repository_root: Path
    ) -> None:
        from app.memory.persistence.schema import initialize_persona_memory_schema

        initialize_persona_memory_schema(runtime_paths, repository_root)

    def build(
        self,
        *,
        runtime_paths: RuntimePaths,
        history: HistoryResources,
        inference: InferenceRuntime,
        environ: Mapping[str, str],
        clock: Callable[[], datetime],
    ) -> None:
        self.clock = clock
        self.runtime_paths = runtime_paths
        self.approved_repository = ApprovedMemoryRepository(
            database_path=runtime_paths.persona_memory_sqlite_path,
            clock=clock,
            uuid_factory=uuid4,
            outbox_uuid_factory=uuid4,
        )
        self.episodic_repository = EpisodicRepository(
            runtime_paths.persona_memory_sqlite_path
        )
        self.legacy_read_repository = CombinedMemoryReadRepository(
            self.approved_repository,
            EpisodicReadRepository(
                self.episodic_repository,
                ConversationSourceGuard(
                    history.config.database_path,
                    clock=clock,
                    retention=history.config.retention,
                ),
            ),
        )
        self.read_repository = WithSemanticReadRepository(
            self.legacy_read_repository
        )
        from app.memory.response_provenance_recorder import (
            ResponseProvenanceRecorder,
        )

        self.provenance_recorder = ResponseProvenanceRecorder(
            runtime_paths.persona_memory_sqlite_path,
            reader=self.legacy_read_repository.episodic,
        )
        self.outbox_repository = IndexOutboxRepository(
            database_path=runtime_paths.persona_memory_sqlite_path,
            clock=clock,
        )
        self.embedder = MemoryInferenceEmbedder(
            router=inference.router,
            settings=inference.settings,
        )
        self.index_sync = MemoryIndexSync(
            approved_repository=self.read_repository,
            outbox_repository=self.outbox_repository,
            chroma_path=runtime_paths.chroma_path,
            runtime_report_dir=runtime_paths.runtime_report_dir,
            embedder=self.embedder,
            embedding_provider_id=self.embedder.provider_id,
            embedding_model_id=self.embedder.model_id,
            clock=clock,
        )
        self.temporary_record_repository = TemporaryProviderRecordRepository(
            database_path=runtime_paths.persona_memory_sqlite_path,
            clock=clock,
            uuid_factory=uuid4,
        )
        self.index_scheduler = MemoryIndexScheduler(self.index_sync)
        self.formation_settings = resolve_memory_formation_settings(environ)
        self.consolidation_settings = resolve_memory_consolidation_settings(environ)

    def build_management(
        self,
        app: FastAPI,
        *,
        privacy: PrivacyResources,
        history: HistoryResources,
        occurred_timezone: str,
    ) -> None:
        legacy = self.legacy_read_repository
        read_repository = self.read_repository
        index_sync = self.index_sync
        approved = self.approved_repository
        temporary = self.temporary_record_repository
        history_repository = history.repository
        clock = self.clock
        paths = self.runtime_paths
        scanner = privacy.scanner
        classifier = privacy.classifier
        assert (
            legacy is not None
            and read_repository is not None
            and index_sync is not None
            and approved is not None
            and temporary is not None
            and history_repository is not None
            and clock is not None
            and paths is not None
            and scanner is not None
            and classifier is not None
        )
        policy = privacy.policy.privacy
        self.semantic_store = SemanticStore(
            repository=SemanticRepository(paths.persona_memory_sqlite_path),
            source_guard=legacy.episodic.source_guard,
            episode_reader=legacy.episodic,
            reviewer=SemanticPrivacyReviewer(
                scanner=scanner, classifier=classifier, policy=policy,
            ),
            clock=clock,
        )
        read_repository.bind(SemanticReadRepository(self.semantic_store))
        app.state.semantic_memory_management = SemanticMemoryManagement(
            self.semantic_store, index_sync
        )
        app.state.semantic_store = self.semantic_store
        self.semantic_published = True
        app.state.persona_memory_provider = PersonaMemoryProvider(
            approved_repository=approved,
            scanner=scanner,
            classifier=classifier,
            admission_evaluator=create_rag_admission_evaluator(policy),
            index_sync=index_sync,
            clock=clock,
        )
        self.persona_published = True
        app.state.episodic_memory_management = EpisodicMemoryManagement(
            reader=legacy.episodic,
            reviewer=EpisodicPrivacyReviewer(
                scanner=scanner, classifier=classifier, policy=policy,
            ),
            clock=clock,
            index_sync=index_sync,
        )
        self.episodic_published = True
        app.state.addon_record_provider = AddonRecordProvider(temporary)
        self.addon_published = True
        self.rag_admission_service = RagAdmissionService(
            conversation_repository=history_repository,
            approved_repository=approved,
            privacy_scanner=scanner,
            semantic_classifier=classifier,
            evaluator=create_rag_admission_evaluator(policy),
            occurred_timezone=occurred_timezone,
            extractor_version=EXTRACTOR_VERSION,
        )
        app.state.rag_admission_service = self.rag_admission_service
        self.rag_published = True

    def build_clients(
        self, inference: InferenceResources, privacy: PrivacyResources
    ) -> None:
        runtime = inference.runtime
        self.extractor_client = StructuredMemoryInferenceClient(
            router=runtime.router,
            settings=runtime.settings,
            caller=InferenceCaller.MEMORY_EXTRACTION,
            target=InferenceTarget.MEMORY_EXTRACTION,
        )
        self.consolidation_client = StructuredMemoryInferenceClient(
            router=runtime.router,
            settings=runtime.settings,
            caller=InferenceCaller.MEMORY_CONSOLIDATION,
            target=InferenceTarget.MEMORY_CONSOLIDATION,
        )
        self.consolidation_classifier_client = InferenceSemanticClassifierClient(
            router=runtime.router,
            settings=runtime.settings,
            model_digest_resolver=lambda model_id, timeout_seconds: (
                runtime.ollama_adapter.resolve_model_digest(
                    model_id,
                    timeout_seconds=timeout_seconds,
                )
            ),
        )
        classifier_client = self.consolidation_classifier_client
        self.consolidation_privacy_classifier = (
            InferenceSemanticPrivacyClassifier(
                client=classifier_client,
                privacy_policy=privacy.policy.privacy,
                model_id=target_model_id(
                    runtime.settings,
                    InferenceTarget.PRIVACY,
                ),
                model_digest_resolver=lambda timeout_seconds: (
                    classifier_client.resolve_model_digest(
                        timeout_seconds=timeout_seconds
                    )
                ),
            )
        )

    def build_formation(
        self,
        *,
        disable: bool,
        history: HistoryResources,
        privacy: PrivacyResources,
        inference: InferenceResources,
        occurred_timezone: str,
        entity_labels: Callable[[str], Mapping[str, str]],
    ) -> None:
        if disable:
            self.formation_scheduler = DisabledFormationScheduler()
            return
        settings = self.formation_settings
        history_repository = history.repository
        extractor_client = self.extractor_client
        semantic_store = self.semantic_store
        episodic_repository = self.episodic_repository
        clock = self.clock
        classifier = privacy.classifier
        scanner = privacy.scanner
        admission_service = self.rag_admission_service
        assert (
            settings is not None
            and history_repository is not None
            and extractor_client is not None
            and semantic_store is not None
            and episodic_repository is not None
            and clock is not None
            and classifier is not None
            and scanner is not None
            and admission_service is not None
        )
        self.candidate_extractor = MemoryCandidateExtractor(
            client=extractor_client,
            settings=settings,
            preferences_only=True,
        )
        self.preference_scheduler = MemoryFormationScheduler(
            worker=MemoryFormationWorker(
                conversation_repository=history_repository,
                extractor=self.candidate_extractor,
                admission_service=admission_service,
                domain_router=None,
            ),
            max_queue_age_seconds=settings.max_queue_age_seconds,
            queue_maxsize=settings.queue_maxsize,
        )
        runtime = inference.runtime
        self.formation_scheduler = CombinedFormationScheduler(
            (
                build_semantic_scheduler(
                    store=semantic_store,
                    runtime=runtime,
                    timezone=occurred_timezone,
                    stale_after=history.config.stale_after,
                )
                if InferenceTarget.SEMANTIC_EXTRACTION
                in runtime.settings.targets
                else self.preference_scheduler
            ),
            build_episodic_scheduler(
                history_path=history.config.database_path,
                repository=episodic_repository,
                clock=clock,
                retention=history.config.retention,
                timezone=occurred_timezone,
                reviewer=EpisodicPrivacyReviewer(
                    scanner=scanner,
                    classifier=classifier,
                    policy=privacy.policy.privacy,
                ),
                client=extractor_client,
                settings=settings,
                runtime=runtime,
                entity_labels=entity_labels,
            ),
        )

    async def start_formation(self) -> None:
        assert self.formation_scheduler is not None
        await self.formation_scheduler.start()
        self.formation_started = True

    def build_consolidation(
        self,
        *,
        inference: InferenceResources,
        history: HistoryResources,
        privacy: PrivacyResources,
        occurred_timezone: str,
    ) -> None:
        settings = self.consolidation_settings
        history_repository = history.repository
        formation_scheduler = self.formation_scheduler
        outbox = self.outbox_repository
        approved = self.approved_repository
        consolidation_client = self.consolidation_client
        consolidation_classifier = self.consolidation_privacy_classifier
        scanner = privacy.scanner
        clock = self.clock
        assert (
            settings is not None
            and history_repository is not None
            and formation_scheduler is not None
            and outbox is not None
            and approved is not None
            and consolidation_client is not None
            and consolidation_classifier is not None
            and scanner is not None
            and clock is not None
        )
        priority = ConsolidationPriorityProbe(
            conversation_repository=history_repository,
            formation_scheduler=formation_scheduler,
            outbox_repository=outbox,
        )
        model_id = target_model_id(
            inference.runtime.settings,
            InferenceTarget.MEMORY_CONSOLIDATION,
        )
        self.consolidation_scheduler = MemoryConsolidationScheduler(
            service=MemoryConsolidationService(
                repository=approved,
                planner=ConsolidationPlanner(
                    client=consolidation_client,
                    max_output_tokens=settings.max_output_tokens,
                    model_id=model_id,
                    prompt_version=CONSOLIDATION_PROMPT_VERSION,
                    policy_version=privacy.policy.policy_version,
                ),
                privacy_reviewer=ConsolidationPrivacyReviewer(
                    scanner=scanner,
                    classifier=consolidation_classifier,
                    evaluator=create_rag_admission_evaluator(
                        privacy.policy.privacy
                    ),
                ),
                batch_size=settings.batch_size,
                llm_timeout_seconds=settings.llm_timeout_seconds,
                clock=clock,
                model_id=model_id,
                prompt_version=CONSOLIDATION_PROMPT_VERSION,
                policy_version=privacy.policy.policy_version,
                reprocess_interval_seconds=settings.interval_seconds,
            ),
            interval_seconds=settings.interval_seconds,
            max_runtime_seconds=settings.max_runtime_seconds,
            priority_probe=lambda: is_consolidation_eligible(
                now=clock().astimezone(ZoneInfo(occurred_timezone)),
                priority=priority.read(),
                idle_seconds=settings.idle_seconds,
                nightly_start_hour=0,
                nightly_end_hour=6,
            ),
        )

    def start_index(self) -> None:
        assert self.index_scheduler is not None
        self.index_scheduler.start()
        self.index_started = True

    async def start_consolidation(self, disable: bool) -> None:
        if disable:
            return
        assert self.consolidation_scheduler is not None
        await self.consolidation_scheduler.start()
        self.consolidation_started = True
