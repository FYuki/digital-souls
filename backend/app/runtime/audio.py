"""音声測定・旧WebSocket audio pipeline・LiveKit資源を所有する。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from app.audio_pipeline import (
    AudioPipelineService,
    AudioRuntimeConfig,
    create_audio_pipeline_service,
    resolve_audio_runtime_config,
)
from app.voice_measurement_memory import POLICY_PATH, record_memory_policy
from app.voice_metrics import (
    JsonlTraceRecorder,
    MeasurementKind,
    cleanup_expired_raw_traces,
    resolve_raw_trace_root,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from app.conversation_history.repository import ConversationHistoryRepository
    from app.livekit_transport.production import (
        ProductionConversationCoreSessionFactory,
        ProductionResources,
    )
    from app.model_settings import ModelSettings
    from app.runtime_paths import RuntimePaths
    from app.screen_perception.service import ScreenPerceptionService
    from app.stt.remote_whisper_client import RemoteWhisperTranscriber
    from app.tts.voicevox_client import VoicevoxClient

VOICE_MEASUREMENT_KIND_ENV = "VOICE_MEASUREMENT_KIND"
VOICE_CONTROLLED_TRACE_PATH_ENV = "VOICE_CONTROLLED_TRACE_PATH"


class AudioResources:
    """voice測定・audio pipeline・LiveKit資源の資源owner。"""

    def __init__(
        self,
        measurement_kind: MeasurementKind,
        trace_recorder: JsonlTraceRecorder | None,
    ) -> None:
        self.measurement_kind = measurement_kind
        self.trace_recorder = trace_recorder
        self.runtime_config: AudioRuntimeConfig | None = None
        self.pipeline_service: AudioPipelineService | None = None
        self.core_transcriber: RemoteWhisperTranscriber | None = None
        self.core_synthesizer: VoicevoxClient | None = None
        self.livekit_resources: ProductionResources | None = None
        self.kind_published = False
        self.recorder_published = False
        self.pipeline_published = False

    @classmethod
    def create_measurement(
        cls,
        environ: Mapping[str, str],
        runtime_paths: RuntimePaths,
        repository_root: Path,
    ) -> AudioResources:
        trace_recorder = None
        measurement_kind: MeasurementKind = "automated_test"
        configured_kind = environ.get(VOICE_MEASUREMENT_KIND_ENV)
        if configured_kind is not None:
            if configured_kind != "controlled_baseline":
                raise ValueError(
                    "VOICE_MEASUREMENT_KIND must be controlled_baseline"
                )
            controlled_trace_path = environ.get(VOICE_CONTROLLED_TRACE_PATH_ENV)
            if controlled_trace_path is None:
                raise ValueError("VOICE_CONTROLLED_TRACE_PATH is required")
            trace_path = Path(controlled_trace_path).resolve()
            data_root = runtime_paths.data_root.resolve()
            if (
                data_root != trace_path.parent
                and data_root not in trace_path.parents
            ):
                raise ValueError(
                    "controlled trace must be inside the controlled data root"
                )
            trace_recorder = JsonlTraceRecorder(trace_path)
            measurement_kind = "controlled_baseline"
        elif runtime_paths.environment_id == "dogfood":
            raw_trace_root = resolve_raw_trace_root(
                repository_root=repository_root,
                data_root=runtime_paths.data_root,
                measurement_kind="dogfood",
            )
            raw_trace_root.mkdir(parents=True, exist_ok=True)
            cleanup_expired_raw_traces(raw_trace_root, now=datetime.now(UTC))
            cleanup_expired_raw_traces(
                raw_trace_root / "sessions", now=datetime.now(UTC)
            )
            trace_recorder = JsonlTraceRecorder(
                raw_trace_root / f"{uuid4()}.jsonl"
            )
            measurement_kind = "dogfood"
        return cls(measurement_kind, trace_recorder)

    def remove_controlled_policy_record(self, runtime_paths: RuntimePaths) -> None:
        if self.measurement_kind == "controlled_baseline":
            (runtime_paths.data_root / POLICY_PATH).unlink(missing_ok=True)

    def publish_measurement(self, app: FastAPI) -> None:
        app.state.voice_measurement_kind = self.measurement_kind
        self.kind_published = True
        if self.trace_recorder is not None:
            app.state.voice_trace_recorder = self.trace_recorder
            self.recorder_published = True

    def build_pipeline(
        self,
        app: FastAPI,
        model_settings: ModelSettings,
        runtime_paths: RuntimePaths,
    ) -> None:
        self.runtime_config = resolve_audio_runtime_config(
            model_settings, runtime_paths
        )
        self.pipeline_service = create_audio_pipeline_service(
            self.runtime_config
        )
        app.state.audio_pipeline_service = self.pipeline_service
        self.pipeline_published = True

    def livekit_enabled(self) -> bool:
        from app.livekit_transport.production import resolve_livekit_settings

        return resolve_livekit_settings() is not None

    def build_core_clients(self) -> None:
        assert self.runtime_config is not None
        from app.stt.remote_whisper_client import RemoteWhisperTranscriber
        from app.tts.voicevox_client import create_voicevox_client

        self.core_transcriber = RemoteWhisperTranscriber(
            self.runtime_config.whisper_base_url
        )
        self.core_synthesizer = create_voicevox_client(
            self.runtime_config.voicevox_base_url
        )

    async def configure(
        self,
        app: FastAPI,
        core_session_factory: ProductionConversationCoreSessionFactory | None,
        *,
        screen_session_revoker: ScreenPerceptionService,
        conversations: ConversationHistoryRepository,
    ) -> None:
        from app.livekit_transport.production import (
            configure_production_resources,
        )

        resources = await configure_production_resources(
            core_session_factory=core_session_factory,
            screen_session_revoker=screen_session_revoker,
            session_trace_recorder=self.trace_recorder,
            measurement_kind=self.measurement_kind,
            conversations=conversations,
        )
        if resources is None:
            return
        self.livekit_resources = resources
        app.state.livekit_room_manager = resources.room_manager
        app.state.livekit_session_repository = resources.session_repository
        app.state.livekit_runtime_manager = resources.runtime_manager
        app.state.livekit_token_signer = resources.token_signer
        app.state.livekit_bootstrap_service = resources.bootstrap_service
        app.state.livekit_core_events = resources.core_events
        app.state.livekit_url = resources.url

    def record_controlled_policy(
        self, runtime_paths: RuntimePaths, *, disabled: bool
    ) -> None:
        if self.measurement_kind == "controlled_baseline":
            record_memory_policy(runtime_paths.data_root, disabled=disabled)
