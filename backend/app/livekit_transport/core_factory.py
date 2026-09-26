"""Conversation Core sessionの組立を所有する。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol
from uuid import UUID, uuid4

from app.characters.loader import IrodoriTtsConfig, TtsConfig, load_tts_config
from app.tts.irodori_client import IrodoriClient, IrodoriRuntimeConfig, IrodoriTtsAdapter
from app.conversation_core import ConversationCoreSession
from app.conversation_core.models import Response
from app.conversation_core.adapters import (
    ConversationHistoryPersistenceAdapter,
    PromptLlmAdapter,
    SpeakerSynthesizer,
    SyncTranscriber,
    SttCapacityError,
    VoicevoxTtsAdapter,
    WhisperSttAdapter,
    ScreenLineageResponseState,
    ResponsePromptState,
)
from app.conversation_core.ports import DeliveryPort, TtsPort
from app.livekit_transport.core_delivery import _ConversationCoreDelivery
from app.livekit_transport.measurement import LiveKitMeasurementSession
from app.livekit_transport.preparation import VoiceModelPreparationError
from app.livekit_transport.production_sdk import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_WIDTH_BYTES,
)
from app.inference.errors import InferenceError
from app.stt.remote_whisper_client import RemoteWhisperError
from app.voice_metrics import MeasurementKind, TraceEvent
from app.screen_perception.provenance import ScreenLineage
from app.prompting.models import BuiltPrompt


class _HistoryService(Protocol):
    def open_session(self, character_id: str, conversation_id: UUID) -> object: ...


class _CoreSessionFactory(Protocol):
    def create(
        self,
        *,
        session_id: str,
        character_id: str,
        conversation_id: UUID,
        delivery: DeliveryPort,
        client_session_id: UUID | None = None,
    ) -> ConversationCoreSession: ...


class _MissingCoreSessionFactory:
    def create(self, **_request: object) -> ConversationCoreSession:
        raise RuntimeError("Conversation Core session factory is required")


class ProductionConversationCoreSessionFactory:
    def __init__(
        self,
        *,
        transcriber: SyncTranscriber,
        synthesizer: SpeakerSynthesizer,
        history_service: _HistoryService,
        irodori_client: IrodoriClient | None = None,
        completed_turn_observer: Callable[[object], None] | None = None,
        response_provenance_recorder: Callable[[object, BuiltPrompt], None] | None = None,
        generate_reply: Callable[[str, object, str], str] | None = None,
        generate_reply_stream: Callable[
            [str, object, str], AsyncIterator[str]
        ] | None = None,
        generate_screen_reply_stream: Callable[
            [
                str, UUID | None, str, UUID, object, str,
                Callable[[tuple[ScreenLineage, ...]], None],
                Callable[[BuiltPrompt], None] | None,
                Response,
            ],
            AsyncIterator[str],
        ] | None = None,
        prepare_inference: Callable[[], Awaitable[bool]] | None = None,
        prepare_prompt: Callable[[str], Awaitable[None]] | None = None,
        measurement_kind: MeasurementKind = "automated_test",
        trace_record: Callable[[TraceEvent], None] | None = None,
        measurement_clock_ns: Callable[[], int] = time.perf_counter_ns,
        on_conversation_interruption: Callable[[str, str, str], None] = lambda _c, _s, _r: None,
    ) -> None:
        self._stt = WhisperSttAdapter(transcriber=transcriber)
        self._synthesizer = synthesizer
        self._irodori_client = irodori_client
        self._history_service = history_service
        self._completed_turn_observer = completed_turn_observer
        self._response_provenance_recorder = response_provenance_recorder
        self._generate_reply = generate_reply
        self._generate_reply_stream = generate_reply_stream
        self._generate_screen_reply_stream = generate_screen_reply_stream
        self._prepare_inference = prepare_inference
        self._prepare_prompt = prepare_prompt
        self._measurement_kind = measurement_kind
        self._trace_record = trace_record
        self._measurement_clock_ns = measurement_clock_ns
        self._on_conversation_interruption = on_conversation_interruption
        if sum(
            source is not None
            for source in (
                generate_reply,
                generate_reply_stream,
                generate_screen_reply_stream,
            )
        ) != 1:
            raise ValueError("exactly one production LLM generation source is required")

    def create(
        self,
        *,
        session_id: str,
        character_id: str,
        conversation_id: UUID,
        delivery: DeliveryPort,
        client_session_id: UUID | None = None,
    ) -> ConversationCoreSession:
        config = load_tts_config(character_id)
        if isinstance(config, IrodoriTtsConfig):
            raise RuntimeError("Irodori requires create_ready before starting a conversation")
        return self._create(
            session_id=session_id, character_id=character_id,
            conversation_id=conversation_id, delivery=delivery,
            client_session_id=client_session_id, tts=self._tts_adapter(config),
        )

    async def create_ready(
        self, *, session_id: str, character_id: str, conversation_id: UUID,
        delivery: DeliveryPort, client_session_id: UUID | None = None,
    ) -> ConversationCoreSession:
        # CCVを一度だけ読み、準備中の編集やtransport再接続で差し替わらないよう固定する。
        tts = self._tts_adapter(load_tts_config(character_id))
        if isinstance(tts, IrodoriTtsAdapter):
            await tts.prepare()
        try:
            await self._stt.prepare_required()
        except (RemoteWhisperError, SttCapacityError) as error:
            raise VoiceModelPreparationError(stage="stt", code=error.error_code) from error
        except Exception as error:
            raise VoiceModelPreparationError(stage="stt", code="stt_preparation_failed") from error
        if self._prepare_inference is not None:
            try:
                prepared = await self._prepare_inference()
                if prepared and self._prepare_prompt is not None:
                    await self._prepare_prompt(character_id)
            except InferenceError as error:
                raise VoiceModelPreparationError(
                    stage="inference", code=f"inference_{error.category.value}",
                ) from error
            except Exception as error:
                raise VoiceModelPreparationError(
                    stage="inference", code="inference_preparation_failed",
                ) from error
        return self._create(
            session_id=session_id, character_id=character_id,
            conversation_id=conversation_id, delivery=delivery,
            client_session_id=client_session_id, tts=tts,
        )

    def _tts_adapter(self, config: TtsConfig) -> TtsPort:
        if isinstance(config, IrodoriTtsConfig):
            return IrodoriTtsAdapter(
                client=self._irodori_client or IrodoriClient(IrodoriRuntimeConfig.from_env()),
                voice=config,
            )
        return VoicevoxTtsAdapter(
            client=self._synthesizer,
            output_sample_rate=PCM_SAMPLE_RATE,
            output_channels=PCM_CHANNELS,
            output_sample_width=PCM_SAMPLE_WIDTH_BYTES,
            speaker_id=config.speaker_id,
        )

    def _create(
        self, *, session_id: str, character_id: str, conversation_id: UUID,
        delivery: DeliveryPort, client_session_id: UUID | None, tts: TtsPort,
    ) -> ConversationCoreSession:
        history_session = self._history_service.open_session(
            character_id, conversation_id
        )
        measurement = LiveKitMeasurementSession(
            session_id=session_id,
            character_id=character_id,
            measurement_kind=self._measurement_kind,
            record=self._trace_record,
            clock_ns=self._measurement_clock_ns,
        )
        if isinstance(delivery, _ConversationCoreDelivery):
            delivery.attach_measurement(measurement)
        screen_lineage_state = ScreenLineageResponseState()
        prompt_state = ResponsePromptState() if self._response_provenance_recorder is not None else None
        return ConversationCoreSession(
            session_id=session_id,
            response_id_factory=lambda: str(uuid4()),
            on_interruption=lambda reason: self._on_conversation_interruption(character_id, str(conversation_id), reason),
            delivery=delivery,
            completion=delivery if isinstance(delivery, _ConversationCoreDelivery) else None,
            cancellation=delivery if isinstance(delivery, _ConversationCoreDelivery) else None,
            persistence=ConversationHistoryPersistenceAdapter(
                history_session=history_session,  # type: ignore[arg-type]
                completed_turn_observer=self._completed_turn_observer,
                screen_lineage_state=screen_lineage_state,
                response_prompt_state=prompt_state,
                response_provenance_recorder=self._response_provenance_recorder,
            ),
            observation=measurement,
            stt=self._stt,
            llm=(
                PromptLlmAdapter(
                    generate_stream_with_response=lambda transcript, response: self._required_generate_screen_reply_stream()(
                        session_id,
                        client_session_id,
                        character_id,
                        conversation_id,
                        history_session,
                        transcript,
                        screen_lineage_state.record,
                        prompt_state.observer() if prompt_state is not None else None,
                        response,
                    )
                )
                if self._generate_screen_reply_stream is not None
                else
                PromptLlmAdapter(
                    generate_reply=lambda transcript: self._required_generate_reply()(
                        character_id, history_session, transcript
                    )
                )
                if self._generate_reply_stream is None
                else PromptLlmAdapter(
                    generate_stream=lambda transcript: self._required_generate_reply_stream()(
                        character_id, history_session, transcript
                    )
                )
            ),
            tts=tts,
        )

    def _required_generate_reply(self) -> Callable[[str, object, str], str]:
        if self._generate_reply is None:
            raise RuntimeError("non-streaming LLM generator is missing")
        return self._generate_reply

    def _required_generate_reply_stream(
        self,
    ) -> Callable[[str, object, str], AsyncIterator[str]]:
        if self._generate_reply_stream is None:
            raise RuntimeError("streaming LLM generator is missing")
        return self._generate_reply_stream

    def _required_generate_screen_reply_stream(
        self,
    ) -> Callable[
        [
            str, UUID | None, str, UUID, object, str,
            Callable[[tuple[ScreenLineage, ...]], None],
            Callable[[BuiltPrompt], None] | None,
            Response,
        ],
        AsyncIterator[str],
    ]:
        if self._generate_screen_reply_stream is None:
            raise RuntimeError("screen-aware streaming LLM generator is missing")
        return self._generate_screen_reply_stream
