from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import time
import struct
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID, uuid4

from fastapi import FastAPI

from app.characters.loader import load_character_card
from app.characters.loader import IrodoriTtsConfig, TtsConfig, load_tts_config
from app.tts.irodori_client import IrodoriClient, IrodoriRuntimeConfig, IrodoriTtsAdapter
from app.conversation_core import ConversationCoreSession, CoreEvent
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
from app.conversation_core.models import Response, ResponseStopResult, ResponseState
from app.livekit_transport.playback_completion import PlaybackCompletionGate
from app.livekit_transport.bootstrap import (
    BOOTSTRAP_TIMEOUT_SECONDS,
    BootstrapService,
    preparation_operation,
    CharacterConversationBindingValidator,
    InMemorySessionBindingRepository,
)
from app.livekit_transport.coordinator import (
    APPLICATION_TOPIC,
    PRIVATE_TOPIC,
    SCREEN_TOPIC,
    ConfirmedOutputStop,
    ProductionSessionCoordinator,
    SessionCoordinatorDependencies,
)
from app.livekit_transport.delivery import CoreNotificationPort, TerminalProtocolError
from app.livekit_transport.errors import RoomCleanupPendingError
from app.livekit_transport.preparation import VoiceModelPreparationError
from app.inference.errors import InferenceError
from app.stt.remote_whisper_client import RemoteWhisperError
from app.livekit_transport.measurement import LiveKitMeasurementSession
from app.livekit_transport.runtime import MicrophoneTrackObserver
from app.livekit_transport.microphone_frames import (
    MicrophoneFrameClock, MICROPHONE_FRAME_MS, MICROPHONE_QUEUE_CAPACITY,
)
from app.livekit_transport.microphone_integrity import AudioIntegrityFault, MicrophoneIntegrity
from app.voice_input.models import check_ready
from app.voice_input.pipeline import AudioInputFault, VoiceInputPipeline
from app.voice_input.session import BackendVoiceInput, InputGrant, SpeechBoundary

from app.livekit_transport.text_input import TextInputReceiver
from app.livekit_transport.stt_audio import (
    PcmCaptureSpan, SttSignalSpan, prepare_stt_audio, stt_preparation_statistics,
)
from app.livekit_transport.response_audio import ResponseAudioTracks
from app.livekit_transport.token import IssuedToken, LiveKitTokenSigner
from app.voice_metrics import JsonlTraceRecorder, MeasurementKind, TraceEvent
from app.voice_session_metrics import SessionMetrics
from app.screen_perception.provenance import ScreenLineage
from app.prompting.models import BuiltPrompt

if TYPE_CHECKING:
    import livekit.api as livekit_api
    import livekit.rtc as rtc


LIVEKIT_URL_ENV = "LIVEKIT_URL"
LIVEKIT_API_KEY_ENV = "LIVEKIT_API_KEY"
LIVEKIT_API_SECRET_ENV = "LIVEKIT_API_SECRET"
PCM_SAMPLE_RATE = 48_000
STT_SAMPLE_RATE = 16_000
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH_BYTES = 2
logger = logging.getLogger(__name__)
STT_MAX_PENDING_UTTERANCES = 3
STT_MAX_OPEN_CAPTURES = STT_MAX_PENDING_UTTERANCES + 1
STT_MAX_UTTERANCE_PCM_BYTES = STT_SAMPLE_RATE * PCM_SAMPLE_WIDTH_BYTES * 30
STT_MAX_PENDING_PCM_BYTES = STT_MAX_PENDING_UTTERANCES * STT_MAX_UTTERANCE_PCM_BYTES
# Whisperはstreaming APIではないため、最初の静音閾値超過から800msのsnapshotを先行認識する。
# 発話前のpre-rollの長さをこの800msへ含めない。
STT_TURN_PREVIEW_PCM_BYTES = int(STT_SAMPLE_RATE * PCM_SAMPLE_WIDTH_BYTES * 0.8)
STT_TURN_PREVIEW_MAX_ATTEMPTS = 3
# 発話確認までの遅れを含め、通知前の語頭をmedia側で最大2秒保持する。
# #150の固定fixtureで確認通知が語頭から1,440ms遅れる条件があり、800msでは語頭を失う。
STT_MICROPHONE_PREROLL_BYTES = STT_SAMPLE_RATE * PCM_SAMPLE_WIDTH_BYTES * 2
# 確認済み発話で300msの静音を受けたら、VAD終了待ちとSTT準備を重ねる。
# 入力の切断・削除・発話確定には使わない。
STT_PREPARATION_QUIET_SAMPLES = STT_SAMPLE_RATE * 300 // 1000


def _livekit_rtc_module() -> Any:
    return importlib.import_module("livekit.rtc")


def _livekit_api_module() -> Any:
    return importlib.import_module("livekit.api")


def _required_int(value: object, field: str) -> int:
    if not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _required_string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{field} must be a list of strings")
    return value


class LiveKitConfigurationError(RuntimeError):
    pass


def resolve_livekit_settings() -> tuple[str, str, str] | None:
    livekit_url = os.environ.get(LIVEKIT_URL_ENV)
    api_key = os.environ.get(LIVEKIT_API_KEY_ENV)
    api_secret = os.environ.get(LIVEKIT_API_SECRET_ENV)
    if livekit_url is None and api_key is None and api_secret is None:
        return None
    if (
        livekit_url is None
        or not livekit_url.strip()
        or api_key is None
        or not api_key.strip()
        or api_secret is None
        or not api_secret.strip()
    ):
        raise LiveKitConfigurationError("LiveKit configuration is incomplete")
    return livekit_url, api_key, api_secret


class ProductionTokenSigner:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self._signer = LiveKitTokenSigner(api_key=api_key, api_secret=api_secret)

    async def issue(self, **request: object) -> str:
        return await self._signer.issue(**request)  # type: ignore[arg-type]

    async def issue_with_expiration(self, **request: object) -> IssuedToken:
        return await self._signer.issue_with_expiration(**request)  # type: ignore[arg-type]

    async def issue_token(self, request: dict[str, object]) -> str:
        return await self.issue(
            identity=str(request["identity"]),
            room=str(request["room"]),
            ttl_seconds=_required_int(request["ttl_seconds"], "ttl_seconds"),
            grant={
                "room_join": True,
                "can_subscribe": bool(request["can_subscribe"]),
                "can_publish": bool(request["can_publish"]),
                "can_publish_data": bool(request["can_publish_data"]),
                "can_publish_sources": _required_string_list(
                    request["can_publish_sources"], "can_publish_sources"
                ),
            },
        )


class ProductionRoomManager:
    def __init__(self, livekit_api: livekit_api.LiveKitAPI) -> None:
        self._api = livekit_api

    async def create(self, room_name: str) -> None:
        api = _livekit_api_module()

        await self._api.room.create_room(api.CreateRoomRequest(name=room_name))

    async def delete(self, room_name: str) -> None:
        api = _livekit_api_module()

        try:
            await self._api.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception as error:
            # 最後のparticipant切断時にLiveKitがRoomを先に削除することがある。
            # その場合だけは所有resourceが既に消えているためcleanup成功とする。
            if (
                getattr(error, "code", None) == "not_found"
                and getattr(error, "status", None) == 404
            ):
                return
            raise

class _ObservationPublisher:
    def __init__(
        self,
        publish: Callable[[bytes, str], Awaitable[None]],
        generation: Callable[[], int],
        schedule: Callable[[Awaitable[None]], None],
    ) -> None:
        self._publish = publish
        self._generation = generation
        self._schedule = schedule

    def record(self, observation: dict[str, object]) -> None:
        frame = {
            "protocol_version": "2.0",
            "type": "microphone_observation",
            "generation": self._generation(),
            **observation,
        }
        self._schedule(
            self._publish(json.dumps(frame, separators=(",", ":")).encode(), PRIVATE_TOPIC)
        )


class ProductionCoreEventInbox:
    """Conversation Core が同一process内で消費する検証済みevent入口。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[bytes], None]] = {}
        self._pending_by_session: dict[str, deque[bytes]] = {}

    def notify(self, payload: bytes) -> None:
        value = json.loads(payload)
        session_id = value.get("session_id") if isinstance(value, dict) else None
        if isinstance(session_id, str):
            handler = self._handlers.get(session_id)
            if handler is not None:
                handler(payload)
                return
            self._pending_by_session.setdefault(session_id, deque()).append(payload)

    def bind(self, session_id: str, handler: Callable[[bytes], None]) -> None:
        if session_id in self._handlers:
            raise RuntimeError("Core event handler is already bound")
        self._handlers[session_id] = handler
        pending = self._pending_by_session.get(session_id)
        while pending:
            handler(pending.popleft())
        self._pending_by_session.pop(session_id, None)

    def unbind(self, session_id: str) -> None:
        self._handlers.pop(session_id, None)
        self._pending_by_session.pop(session_id, None)


class _RtcAudioSource(Protocol):
    async def capture_frame(self, frame: rtc.AudioFrame) -> None: ...

    def clear_queue(self) -> None: ...


class _LiveKitPcmAudioSource:
    def __init__(self, source: _RtcAudioSource) -> None:
        self._source = source

    async def begin_response(self, response_id: str) -> None:
        """単一sourceのadapter。productionではResponseAudioTracksがtrackを分離する。"""

    async def publish(self, pcm: bytes, *, response_id: str) -> int:
        rtc_module = _livekit_rtc_module()

        samples_per_channel = len(pcm) // (
            PCM_SAMPLE_WIDTH_BYTES * PCM_CHANNELS
        )
        frame: rtc.AudioFrame = rtc_module.AudioFrame(
            pcm,
            PCM_SAMPLE_RATE,
            PCM_CHANNELS,
            samples_per_channel,
        )
        await self._source.capture_frame(frame)
        return time.monotonic_ns()

    async def finish_response(self, response_id: str) -> None:
        """既存adapterのnative bufferは従来どおりsourceが所有する。"""

    def clear(self, response_id: str | None = None) -> None:
        self._source.clear_queue()


class _HistoryService(Protocol):
    def open_session(self, character_id: str, conversation_id: UUID) -> object: ...


class _ScreenSessionRevoker(Protocol):
    async def revoke_client(self, client_session_id: UUID, reason: str) -> None: ...


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
                    generate_stream=lambda transcript: self._required_generate_screen_reply_stream()(
                        session_id,
                        client_session_id,
                        character_id,
                        conversation_id,
                        history_session,
                        transcript,
                        screen_lineage_state.record,
                        prompt_state.observer() if prompt_state is not None else None,
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
        ],
        AsyncIterator[str],
    ]:
        if self._generate_screen_reply_stream is None:
            raise RuntimeError("screen-aware streaming LLM generator is missing")
        return self._generate_screen_reply_stream


class _ConversationCoreDelivery:
    def __init__(
        self,
        *,
        coordinator: ProductionSessionCoordinator,
        audio_source: _LiveKitPcmAudioSource | ResponseAudioTracks,
        character_participant_id: str,
        character_id: str,
        user_participant_id: str | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._audio_source = audio_source
        self._character_speaker = {
            "participant_id": character_participant_id,
            "role": "character",
            "character_id": character_id,
        }
        self._user_speaker = (
            {"participant_id": user_participant_id, "role": "user"}
            if user_participant_id is not None
            else None
        )
        self._session_id: str | None = None
        self._completion_gate = PlaybackCompletionGate()
        self._completed_output_responses: set[str] = set()
        self._first_audio_observed: set[str] = set()
        self._first_text_observed: set[str] = set()
        self._measurement: LiveKitMeasurementSession | None = None

    @property
    def measurement(self) -> LiveKitMeasurementSession | None:
        return self._measurement

    def attach_measurement(self, measurement: LiveKitMeasurementSession) -> None:
        if self._measurement is measurement:
            return
        if self._measurement is not None and self._measurement is not measurement:
            raise RuntimeError("delivery measurement is already attached")
        self._measurement = measurement
        if isinstance(self._audio_source, ResponseAudioTracks):
            self._audio_source.set_observer(lambda name, response_id: measurement.record_response_event(
                response_id=response_id, name=name, stage="transport",
            ))

    async def publish(self, event: CoreEvent) -> None:
        if self._session_id is None:
            self._session_id = event.session_id
        elif self._session_id != event.session_id:
            raise ValueError("delivery event belongs to another session")
        if (
            event.response_id is not None
            and event.source_utterance_ids is not None
            and self._measurement is not None
        ):
            self._measurement.bind_response(
                response_id=event.response_id,
                source_utterance_ids=event.source_utterance_ids,
                source_inputs=event.source_inputs,
            )
        if event.type == "response_started":
            if event.response_id is None or event.source_utterance_ids is None:
                raise ValueError(
                    "response_started requires response id and source utterances"
                )
            if self._measurement is not None:
                self._measurement.bind_response(
                    response_id=event.response_id,
                    source_utterance_ids=event.source_utterance_ids,
                    source_inputs=event.source_inputs,
                )
                self._measurement.record_response_event(
                    response_id=event.response_id,
                    name="response_decision",
                    stage="turn",
                )
            self._coordinator.begin_response(response_id=event.response_id)
            await self._audio_source.begin_response(event.response_id)
        if (
            event.type == "utterance_finalized"
            and event.utterance_id is not None
            and self._measurement is not None
        ):
            self._measurement.record_utterance_event(
                utterance_id=event.utterance_id,
                name="utterance_finalized",
                stage="stt",
            )
            if event.should_response is False:
                self._measurement.bind_utterance_outcome(
                    utterance_id=event.utterance_id,
                    outcome="excluded",
                    reason_code="response_not_requested",
                )
        if (
            event.type in {"utterance_discarded", "error"}
            and event.utterance_id is not None
            and self._measurement is not None
        ):
            outcome: Literal["failure", "excluded"] = (
                "failure" if event.type == "error" else "excluded"
            )
            reason_code = (
                event.error_code if event.type == "error" else event.reason
            )
            self._measurement.bind_utterance_outcome(
                utterance_id=event.utterance_id,
                outcome=outcome,
                reason_code=reason_code or f"{event.type}_without_reason",
            )
        if (
            event.type == "response_delta"
            and event.response_id is not None
            and event.response_id not in self._first_text_observed
        ):
            self._first_text_observed.add(event.response_id)
            if self._measurement is not None:
                self._measurement.record_response_event(
                    response_id=event.response_id,
                    name="first_text_delta",
                    stage="llm",
                )
        if event.type == "response_audio_segment":
            if (
                event.audio is None
                or event.response_id is None
                or event.audio_sequence is None
            ):
                raise ValueError("audio event requires response metadata and PCM bytes")
            response_id = event.response_id
            first_audio = response_id not in self._first_audio_observed
            if first_audio and self._measurement is not None:
                self._measurement.record_response_event(
                    response_id=response_id,
                    name="tts_completed",
                    stage="tts",
                )
                self._measurement.record_response_event(
                    response_id=response_id,
                    name="first_audio_generated",
                    stage="tts",
                )
            await self._coordinator.send_logical_audio_segment(
                response_id=event.response_id,
                # private playback evidenceは0始まり、Core contractは1始まり。
                audio_sequence=event.audio_sequence - 1,
                pcm_sample_count=len(event.audio)
                // (PCM_SAMPLE_WIDTH_BYTES * PCM_CHANNELS),
            )
            await self._coordinator.send_core(self._voice_payload(event))
            first_capture_ns = await self._audio_source.publish(event.audio, response_id=response_id)
            await self._observe_first_audio_out(response_id, first_capture_ns)
            return
        if (
            event.type == "response_cancelled"
            and event.response_id is not None
            and self._measurement is not None
        ):
            if event.terminal_state_bounds_ns is not None:
                for name, timestamp in zip(("cancel_state_lower", "cancel_state_upper"),
                                           event.terminal_state_bounds_ns, strict=True):
                    self._measurement.record_response_event(
                        response_id=event.response_id, name=name, stage="response", timestamp=timestamp,
                    )
            self._measurement.record_response_event(
                response_id=event.response_id,
                name="response_excluded",
                stage="response",
                outcome="excluded",
                reason_code=event.reason or "response_cancelled",
            )
            self._measurement.record_response_event(
                response_id=event.response_id, name="response_cancelled", stage="response",
                outcome="excluded", reason_code=event.reason or "response_cancelled",
            )
        if (
            event.type == "response_privacy_skipped"
            and event.response_id is not None
            and self._measurement is not None
        ):
            self._measurement.record_response_event(
                response_id=event.response_id,
                name="response_excluded",
                stage="response",
                outcome="excluded",
                reason_code="privacy_skip",
            )
        if event.type == "response_completed" and event.response_id is not None:
            if event.response_id not in self._completed_output_responses:
                await self._finish_audio(event.response_id)
        if event.type in {"response_cancelled", "response_failed", "response_privacy_skipped"}:
            self._audio_source.clear(event.response_id)
        if event.type == "response_privacy_skipped":
            if event.source_utterance_ids is None:
                raise ValueError("privacy event requires source utterance ids")
            for utterance_id in event.source_utterance_ids:
                await self._coordinator.send_core(
                    self._voice_payload(event, utterance_id=utterance_id)
                )
        await self._coordinator.send_core(self._voice_payload(event))

    async def stop_response(self, response: Response) -> ResponseStopResult:
        if not isinstance(self._audio_source, ResponseAudioTracks):
            raise RuntimeError("output stop confirmation requires response audio tracks")
        if self._measurement is not None:
            self._measurement.record_response_event(
                response_id=response.response_id, name="output_stop_requested", stage="transport",
            )
        stopped, prefix = await asyncio.gather(
            self._audio_source.stop_response(response.response_id),
            self._coordinator.request_output_stop(response.response_id),
            return_exceptions=True,
        )
        if isinstance(stopped, BaseException) or not isinstance(prefix, ConfirmedOutputStop):
            raise RuntimeError("response output stop was not confirmed")
        if self._measurement is not None:
            self._measurement.record_response_event(
                response_id=response.response_id, name="output_stop_confirmed", stage="transport",
                # この確認eventのIDを要求nonceと揃え、privateな一次記録同士だけで照合する。
                event_id=prefix.request_id,
            )
        return ResponseStopResult(prefix.last_played_audio_sequence)

    async def finish_response(self, response: Response) -> None:
        if not response.audio_segments:
            self._completed_output_responses.add(response.response_id)
            return
        if isinstance(self._audio_source, ResponseAudioTracks):
            await self._completion_gate.wait(
                response.response_id, len(response.audio_segments),
                lambda: self._finish_audio(response.response_id),
            )
        else:
            await self._finish_audio(response.response_id)
        self._completed_output_responses.add(response.response_id)

    def confirm_response_playback(self, response_id: str, last_audio_sequence: int) -> bool:
        return self._completion_gate.confirm(response_id, last_audio_sequence)

    async def _finish_audio(self, response_id: str) -> None:
        first_capture_ns = await self._audio_source.finish_response(response_id)
        if isinstance(self._audio_source, ResponseAudioTracks):
            statistics = self._audio_source.statistics(response_id)
            if self._measurement is not None:
                for name, value in statistics.items():
                    self._measurement.record_response_event(
                        response_id=response_id, name=name, stage="transport", value=value,
                    )
            await self._coordinator.send_response_audio_finished(
                response_id=response_id,
                input_sample_count=statistics["response_audio_input_samples"],
                captured_sample_count=statistics["response_audio_captured_samples"],
                padding_sample_count=statistics["response_audio_padding_samples"],
            )
        await self._observe_first_audio_out(response_id, first_capture_ns)

    async def _observe_first_audio_out(self, response_id: str | None, timestamp_ns: int | None) -> None:
        if timestamp_ns is None or response_id is None or response_id in self._first_audio_observed:
            return
        self._first_audio_observed.add(response_id)
        if self._measurement is not None:
            self._measurement.record_response_event(
                response_id=response_id, name="first_audio_out", stage="transport", timestamp=timestamp_ns,
            )
        await self._coordinator.send_core(json.dumps({
            "type": "observation", "protocol_version": "2.0", "event_id": str(uuid4()),
            "session_id": self._session_id, "response_id": response_id,
            "measurement": "first_audio_out", "timestamp": str(timestamp_ns),
            "clock_domain": "server_monotonic", "unit": "nanosecond",
        }, separators=(",", ":")).encode())

    def _voice_payload(
        self, event: CoreEvent, *, utterance_id: str | None = None
    ) -> bytes:
        payload: dict[str, object] = {
            "protocol_version": "2.0",
            "event_id": str(uuid4()),
            "session_id": event.session_id,
            "monotonic_timestamp_ms": int(time.monotonic() * 1000),
        }
        if event.type == "response_started":
            if event.source_utterance_ids is None:
                raise ValueError("response_started requires source utterance ids")
            payload.update(
                type=event.type,
                response_id=event.response_id,
                speaker=self._character_speaker,
                source_utterance_ids=list(event.source_utterance_ids),
                source_inputs=[
                    {"input_id": item.input_id, "source": item.source}
                    for item in event.source_inputs
                ] if event.source_inputs is not None else [
                    {"input_id": item, "source": "speech"}
                    for item in event.source_utterance_ids
                ],
            )
            if event.history_turn_id is not None:
                payload["history_turn_id"] = event.history_turn_id
        elif event.type == "utterance_finalized":
            if (
                event.utterance_id is None
                or event.transcript is None
                or event.should_response is None
                or self._user_speaker is None
            ):
                raise ValueError(
                    "utterance_finalized requires user speaker and transcript"
                )
            payload.update(
                type=event.type,
                utterance_id=event.utterance_id,
                speaker=self._user_speaker,
                transcript=event.transcript,
                should_response=event.should_response,
            )
        elif event.type == "turn_decision":
            if (
                event.utterance_id is None
                or event.response_id is None
                or event.decision not in {"backchannel", "take_turn", "indeterminate"}
                or event.final is None
            ):
                raise ValueError("turn_decision requires decision metadata")
            payload.update(
                type=event.type,
                utterance_id=event.utterance_id,
                response_id=event.response_id,
                decision=event.decision,
                final=event.final,
            )
        elif event.type == "error":
            if (
                event.utterance_id is None
                or event.classification is None
                or event.error_code is None
                or event.user_state is None
            ):
                raise ValueError("error event requires utterance error metadata")
            payload.update(
                type=event.type,
                utterance_id=event.utterance_id,
                classification=event.classification,
                error_code=event.error_code,
                user_state=event.user_state,
            )
        elif event.type == "utterance_discarded":
            if event.utterance_id is None or event.reason is None:
                raise ValueError(
                    "utterance_discarded requires utterance id and reason"
                )
            payload.update(
                type=event.type,
                utterance_id=event.utterance_id,
                reason=event.reason,
            )
        elif event.type == "response_delta":
            if event.text_range is None:
                raise ValueError("response_delta requires text range")
            payload.update(
                type=event.type,
                response_id=event.response_id,
                text_sequence=event.text_sequence,
                text=event.text,
                text_range={
                    "start": event.text_range[0],
                    "end": event.text_range[1],
                },
            )
        elif event.type == "response_audio_segment":
            if event.text_range is None:
                raise ValueError("response_audio_segment requires text range")
            payload.update(
                type=event.type,
                response_id=event.response_id,
                audio_sequence=event.audio_sequence,
                text_range={
                    "start": event.text_range[0],
                    "end": event.text_range[1],
                },
            )
        elif event.type == "response_completed":
            payload.update(
                type=event.type,
                response_id=event.response_id,
                last_text_sequence=event.last_text_sequence,
                last_audio_sequence=event.last_audio_sequence,
            )
        elif event.type == "response_cancelled":
            payload.update(type=event.type, response_id=event.response_id, reason=event.reason)
        elif event.type == "response_failed":
            error_code = (
                event.reason
                if event.reason is not None
                else "conversation_core_failed"
            )
            payload.update(
                type=event.type,
                response_id=event.response_id,
                error_code=error_code,
                recoverable=True,
            )
        elif event.type == "response_privacy_skipped":
            if utterance_id is not None:
                # 既存の音声入力破棄通知を維持する。
                payload.update(type="utterance_discarded", utterance_id=utterance_id, reason="privacy")
            else:
                sources = event.source_inputs
                if not sources:
                    raise ValueError("privacy event requires source inputs")
                payload.update(
                    type=event.type, response_id=event.response_id,
                    source_inputs=[{"input_id": item.input_id, "source": item.source} for item in sources],
                )
        else:
            raise ValueError(f"unsupported Core event type: {event.type}")
        return json.dumps(payload, separators=(",", ":")).encode()


@dataclass
class _UserAudioCapture:
    utterance_id: str
    interrupted_response_id: str | None = None
    pcm: bytearray = field(default_factory=bytearray)
    finalized: bool = False
    integrity_verified: bool = False
    capacity_exceeded: bool = False
    preview_attempts: int = 0
    preview_complete: bool = False
    preview_last_signal_samples: int = 0
    preview_signal: SttSignalSpan = field(default_factory=SttSignalSpan)
    received_span: PcmCaptureSpan = field(default_factory=PcmCaptureSpan)
    media_offset_samples: int | None = None
    vad_started_sample: int | None = None
    vad_active_end_sample: int | None = None
    vad_detected_end_sample: int | None = None
    media_end_anchor_valid: bool = False
    preparation_started: bool = False
    quiet_samples: int = 0


class _ConversationCoreBridge:
    def __init__(
        self,
        session: ConversationCoreSession,
        schedule: Callable[[Awaitable[None]], None],
        stop_audio: Callable[[str], None] = lambda _response_id: None,
        confirm_response_playback: Callable[[str, int], bool] = lambda _response_id, _sequence: False,
        measurement: LiveKitMeasurementSession | None = None,
        session_metrics: SessionMetrics | None = None,
        text_input: TextInputReceiver | None = None,
        publish_audio_event: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        authorize_microphone: Callable[[str], Awaitable[bool]] | None = None,
        verify_audio_integrity: Callable[[str, int, int], Awaitable[None]] | None = None,
        user_participant_id: str | None = None,
    ) -> None:
        self._session = session
        self._text_input = text_input
        self._voice_input: BackendVoiceInput | None = None
        self._publish_audio_event = publish_audio_event
        self._authorize_microphone = authorize_microphone
        self._verify_audio_integrity = verify_audio_integrity
        self._user_participant_id = user_participant_id
        self._playback_response_id: str | None = None
        self._schedule = schedule
        self._stop_audio = stop_audio
        self._confirm_response_playback = confirm_response_playback
        self._measurement = measurement
        self._session_metrics = session_metrics
        self._user_audio_captures: deque[_UserAudioCapture] = deque()
        self._pending_transcriptions: deque[tuple[str, bytes, str | None]] = deque()
        self._pending_transcription_bytes = 0
        self._transcription_active = False
        self._transcription_epoch = 0
        self._microphone_preroll = bytearray()
        self._microphone_received_bytes = 0
        self._control_lock = asyncio.Lock()
        self._text_focus_suppressed = False
        self._manual_input_muted = False

    def notify(self, payload: bytes) -> None:
        event = json.loads(payload)
        if event["type"] in {"speech_started", "speech_stopped"}:
            raise TerminalProtocolError("speech boundaries are owned by Backend")
        if event["type"] == "audio_input_open_requested":
            self._schedule(self._open_audio(event))
            return
        if event["type"] in {"audio_input_suppression_changed", "session_muted", "session_resumed", "user_text_submitted"}:
            if self._voice_input is None:
                raise TerminalProtocolError("audio input is not ready")
            reason = "text_priority" if event["type"] == "user_text_submitted" else "input_suppressed"
            if not self._voice_input.suppress(reason=reason, input_revision=event["input_revision"]):
                if event["type"] != "user_text_submitted":
                    return
                self._voice_input.suppress(reason="text_priority")
        if event["type"] in {"session_disconnected", "session_reconnected"} and self._voice_input is not None:
            self._voice_input.suppress(reason="disconnect")
            self._microphone_preroll.clear()
        if event["type"] == "audio_input_suppression_changed":
            self._text_focus_suppressed = bool(event["suppressed"])
            self._apply_audio_input_gate()
            return
        if event["type"] in {"session_muted", "session_resumed"}:
            self._manual_input_muted = event["type"] == "session_muted"
            self._apply_audio_input_gate()
            return
        if event["type"] in {"user_text_submitted", "user_input_result_requested"}:
            if self._text_input is None:
                raise TerminalProtocolError("text input receiver is not connected")
            # 受付が非同期処理中でも照合要求はprocessingの結果へ到達できる。
            self._schedule(self._text_input.receive(event))
            return
        if event["type"] == "observation":
            if self._measurement is not None:
                self._measurement.record_client_observation(event)
            return
        self._schedule(self._receive_serialized(event))

    async def prepare_audio(self) -> None:
        def prepare() -> VoiceInputPipeline:
            check_ready()
            return VoiceInputPipeline()
        task = asyncio.create_task(asyncio.to_thread(prepare))
        try:
            pipeline = await asyncio.shield(task)
        except BaseException:
            def release(done: asyncio.Task[VoiceInputPipeline]) -> None:
                if not done.cancelled() and done.exception() is None:
                    done.result().close()
            task.add_done_callback(release)
            raise
        self._voice_input = BackendVoiceInput(
            pipeline, on_frame=lambda frame: self.receive_microphone(frame.pcm),
            on_started=self._audio_started, on_stopped=self._audio_stopped,
            on_discarded=self._audio_discarded,
        )

    def _audio_event(self, kind: str, **fields: object) -> dict[str, object]:
        return {"type": kind, "protocol_version": "2.0", "event_id": str(uuid4()),
                "session_id": self._session.session_id,
                "monotonic_timestamp_ms": time.monotonic_ns() // 1_000_000, **fields}

    async def _publish_audio(self, event: dict[str, object]) -> None:
        if self._publish_audio_event is None:
            raise RuntimeError("audio event delivery is not connected")
        await self._publish_audio_event(event)

    def _speech_event(self, kind: str, boundary: SpeechBoundary) -> dict[str, object]:
        detection = boundary.detection
        return self._audio_event(
            kind, utterance_id=boundary.utterance_id,
            speaker={"role": "user", "participant_id": self._user_participant_id},
            input_generation=boundary.grant.input_generation, track_sid=boundary.grant.track_sid,
            start_sample=detection.started_sample, detected_sample=detection.detected_sample,
            active_end_sample=detection.active_end_sample, sample_rate=STT_SAMPLE_RATE,
            clock_domain="server_monotonic",
        )

    def _audio_started(self, boundary: SpeechBoundary) -> None:
        event = self._speech_event("speech_started", boundary)
        active = self._session.active_response
        response_id = active.response_id if active is not None else self._playback_response_id
        if response_id is not None:
            try:
                response = self._session.response(response_id)
            except KeyError:
                response_id = None
            else:
                if response.state not in {ResponseState.IN_PROGRESS, ResponseState.COMPLETED}:
                    response_id = None
        if response_id is not None:
            event["response_id"] = response_id
        self._begin_capture(event)
        self._schedule(self._publish_audio(event))

    async def _audio_stopped(self, boundary: SpeechBoundary) -> None:
        capture = next((item for item in self._user_audio_captures
                        if item.utterance_id == boundary.utterance_id), None)
        if capture is None:
            return
        # 発話終了位置は既にPCMで確認済み。focus抑止はこの区間を取り消さない。
        capture.finalized = True
        # await前に、この終了frameまでの受信連番とVADのtrack sample位置を固定する。
        detection = boundary.detection
        capture.vad_active_end_sample = detection.active_end_sample
        capture.vad_detected_end_sample = detection.detected_sample
        capture.media_end_anchor_valid = (
            capture.media_offset_samples is not None
            and capture.vad_started_sample == detection.started_sample
            and self._microphone_received_bytes // 2 + capture.media_offset_samples
            == detection.detected_sample
        )
        try:
            await self._publish_audio(self._speech_event("speech_stopped", boundary))
            if self._measurement is not None:
                self._measurement.record_utterance_event(
                    utterance_id=boundary.utterance_id, name="vad_speech_end", stage="vad",
                )
            if self._verify_audio_integrity is None:
                raise AudioInputFault("audio_integrity_unavailable")
            await self._verify_audio_integrity(
                boundary.grant.track_sid, boundary.detection.started_sample, boundary.detection.detected_sample,
            )
        except AudioInputFault as error:
            if self._measurement is not None and isinstance(error, AudioIntegrityFault):
                for name, value in error.statistics.items():
                    self._measurement.record_utterance_event(
                        utterance_id=boundary.utterance_id, name=name, stage="input_integrity",
                        value=value,
                    )
            self._audio_discarded(boundary.utterance_id, error.code)
            # 先行発話が失敗しても、別世代で終了・検証済みの後続を滞留させない。
            await self._finalize_user_audio_if_ready()
            return
        except BaseException:
            # track交換や切断はreaderの確定待ちも取り消す。未検証の先頭captureを
            # 残すと、別trackで検証済みの後続発話までSTTへ進めなくなる。
            # text優先で既に破棄済みなら重ねて通知しない。取消・元例外は伝播する。
            if any(item is capture for item in self._user_audio_captures):
                self._audio_discarded(boundary.utterance_id, "audio_integrity_unavailable")
                self._schedule(self._finalize_user_audio_if_ready())
            raise
        if not any(item is capture for item in self._user_audio_captures):
            return
        # BE自身がPCM境界を検出するため、FE通知後のmedia tail待機は不要。
        capture.integrity_verified = True
        await self._finalize_user_audio_if_ready()

    def _audio_discarded(self, utterance_id: str | None, reason: str) -> None:
        # 終了済み発話の検証失敗は、新trackで蓄積中のprerollを所有しない。
        if utterance_id is None or any(
            item.utterance_id == utterance_id and not item.finalized
            for item in self._user_audio_captures
        ):
            self._microphone_preroll.clear()
        if utterance_id is not None:
            self._user_audio_captures = deque(
                item for item in self._user_audio_captures if item.utterance_id != utterance_id
            )
            normalized = {
                "invalid_audio_frame": "invalid_audio",
                "vad_backlog_exceeded": "input_capacity_exceeded",
                "vad_reset_required": "vad_unavailable",
                "vad_processing_failed": "vad_unavailable",
                "microphone_track_unavailable": "audio_gap",
            }.get(reason, reason)
            self._schedule(self._discard_capture(utterance_id, normalized))
        if reason not in {"input_suppressed", "input_replaced", "input_closed", "text_priority", "disconnect", "microphone_track_unavailable"}:
            self._schedule(self._publish_audio(self._audio_event(
                "error", classification="recoverable", error_code="audio_input_repeat_required",
                user_state="listening",
            )))

    async def _open_audio(self, event: dict[str, object]) -> None:
        request_id, track_sid = str(event["event_id"]), str(event["track_sid"])
        try:
            source = self._voice_input
            if source is None:
                raise AudioInputFault("vad_unavailable")
            if self._audio_input_suppressed:
                raise AudioInputFault("input_suppressed")
            if self._authorize_microphone is None or not await self._authorize_microphone(track_sid):
                raise AudioInputFault("microphone_track_unavailable")
            if self._audio_input_suppressed:
                raise AudioInputFault("input_suppressed")
            if self._voice_input is not source:
                raise AudioInputFault("vad_unavailable")
            grant = await source.open(
                track_sid=track_sid, request_id=request_id, input_revision=int(str(event["input_revision"])),
            )
            self._microphone_preroll.clear()
            await self._publish_audio(self._audio_event(
                "audio_input_opened", request_event_id=request_id, track_sid=grant.track_sid,
                input_generation=grant.input_generation, input_revision=grant.input_revision,
            ))
        except AudioInputFault as error:
            await self._publish_audio(self._audio_event(
                "audio_input_rejected", request_event_id=request_id, reason=error.code,
            ))

    async def receive_microphone_frame(self, pcm: bytes, *, start_sample: int, track_sid: str) -> None:
        source = self._voice_input
        if source is None or self._audio_input_suppressed:
            return
        grant = source.grant
        if grant is None or grant.track_sid != track_sid:
            return
        try:
            await source.receive(pcm, start_sample=start_sample, grant=grant)
        except AudioInputFault:
            # resetの失敗は当該入力だけを停止する。旧世代の失敗で新入力を止めない。
            if self._voice_input is source and source.revision == grant.input_revision:
                self._audio_unavailable(grant)

    def _audio_unavailable(self, grant: InputGrant) -> None:
        self._schedule(self._publish_audio(self._audio_event(
            "error", classification="recoverable", error_code="audio_input_unavailable",
            user_state="muted", track_sid=grant.track_sid,
            input_generation=grant.input_generation, input_revision=grant.input_revision,
        )))

    def close_microphone_track(
        self, track_sid: str, *, reason: str = "microphone_track_unavailable",
    ) -> None:
        source = self._voice_input
        if source is not None and source.grant is not None and source.grant.track_sid == track_sid:
            grant = source.grant
            source.suppress(reason=reason)
            self._microphone_preroll.clear()
            if reason == "microphone_track_unavailable":
                # 無音中のreader終了にも通知し、聴取中表示を残さない。
                self._audio_unavailable(grant)

    async def close_audio(self) -> None:
        source, self._voice_input = self._voice_input, None
        if source is not None:
            await source.close()

    def _begin_capture(self, event: dict[str, object]) -> None:
        if self._audio_input_suppressed:
            return
        utterance_id = str(event["utterance_id"])
        if self._measurement is not None:
            interrupted_response_id = event.get("response_id")
            if isinstance(interrupted_response_id, str):
                self._measurement.bind_interruption(
                    utterance_id=utterance_id, response_id=interrupted_response_id,
                )
            self._measurement.record_utterance_event(
                utterance_id=utterance_id,
                name="speech_started",
                stage="vad",
            )
        open_captures = sum(
            not capture.finalized for capture in self._user_audio_captures
        )
        if open_captures >= STT_MAX_OPEN_CAPTURES:
            oldest_open = next(
                capture
                for capture in self._user_audio_captures
                if not capture.finalized
            )
            self._user_audio_captures.remove(oldest_open)
            self._schedule(
                self._discard_capture(oldest_open.utterance_id)
            )
        capture = _UserAudioCapture(
            utterance_id=utterance_id,
            interrupted_response_id=(
                str(event["response_id"])
                if isinstance(event.get("response_id"), str)
                else None
            ),
        )
        detected, started = event.get("detected_sample"), event.get("start_sample")
        if (event.get("type") == "speech_started"
                and type(detected) is int and type(started) is int
                and 0 <= started <= detected):
            # on_frameがこのconfirmed frameをcaptureへ渡した直後に呼ばれる。
            # 世代間でbridgeの受信連番は継続するため、offsetは負にもなり得る。
            capture.media_offset_samples = detected - self._microphone_received_bytes // 2
            capture.vad_started_sample = started
        capture.received_span.append(
            self._microphone_received_bytes - len(self._microphone_preroll),
            len(self._microphone_preroll),
        )
        capture.pcm.extend(self._microphone_preroll)
        self._microphone_preroll.clear()
        self._user_audio_captures.append(capture)
        self._consider_stt_preparation(capture, bytes(capture.pcm))
        return

    @property
    def _audio_input_suppressed(self) -> bool:
        return self._text_focus_suppressed or self._manual_input_muted

    def invalidate_unfinalized_audio(self) -> tuple[str, ...]:
        """新しいtext受付時に、未確定capture/待機STTを次の入力世代から切り離す。"""
        discarded = tuple(dict.fromkeys([
            *(capture.utterance_id for capture in self._user_audio_captures),
            *(utterance_id for utterance_id, _, _ in self._pending_transcriptions),
        ]))
        self._transcription_epoch += 1
        self._transcription_active = False
        self._user_audio_captures.clear()
        self._pending_transcriptions.clear()
        self._pending_transcription_bytes = 0
        self._microphone_preroll.clear()
        return discarded

    def _apply_audio_input_gate(self) -> None:
        if not self._audio_input_suppressed:
            return
        # mediaとcontrolの到着順は異なる。抑止中のframe/prerollを復帰後へ渡さない。
        self._microphone_preroll.clear()
        for capture in tuple(self._user_audio_captures):
            if not capture.finalized:
                self._user_audio_captures.remove(capture)
                self._schedule(self._discard_capture(
                    utterance_id=capture.utterance_id, reason="input_suppressed",
                ))

    async def _discard_capture(
        self, utterance_id: str, reason: str = "input_capacity_exceeded",
    ) -> None:
        await self._session.discard_utterance(
            utterance_id=utterance_id,
            reason=reason,
        )

    async def _receive_serialized(self, event: dict[str, object]) -> None:
        async with self._control_lock:
            await self._receive(event)

    def receive_microphone(self, pcm: bytes) -> None:
        received_start_byte = self._microphone_received_bytes
        self._microphone_received_bytes += len(pcm)
        if self._audio_input_suppressed:
            return
        capture = next(
            (item for item in reversed(self._user_audio_captures) if not item.finalized),
            None,
        )
        if capture is None:
            # BEがPCMから確定した終了境界を越える音声は次の入力のprerollにする。
            # 終了通知・欠落確認の待機中にも、確定済みcaptureへ追加しない。
            self._microphone_preroll.extend(pcm)
            excess = len(self._microphone_preroll) - STT_MICROPHONE_PREROLL_BYTES
            if excess > 0:
                del self._microphone_preroll[:excess]
            return
        if self._measurement is not None:
            self._measurement.record_utterance_event(
                utterance_id=capture.utterance_id,
                name="user_audio_received",
                stage="transport",
            )
        if len(capture.pcm) + len(pcm) > STT_MAX_UTTERANCE_PCM_BYTES:
            capture.capacity_exceeded = True
        elif not capture.capacity_exceeded:
            capture.received_span.append(received_start_byte, len(pcm))
            capture.pcm.extend(pcm)
        self._consider_stt_preparation(capture, pcm)
        self._consider_turn_preview(capture)

    def _consider_turn_preview(self, capture: _UserAudioCapture) -> None:
        if (capture.interrupted_response_id is None or capture.preview_complete
                or capture.preview_attempts >= STT_TURN_PREVIEW_MAX_ATTEMPTS
                or capture.finalized or capture.capacity_exceeded or self._transcription_active
                or not getattr(self._session, "accepting_input", True)
                or not any(item is capture for item in self._user_audio_captures)):
            return
        signal_samples = capture.preview_signal.sample_count(capture.pcm)
        if (signal_samples - capture.preview_last_signal_samples) * PCM_SAMPLE_WIDTH_BYTES < STT_TURN_PREVIEW_PCM_BYTES:
            return
        # 相槌で始まる長い発話は追加800msで再評価する。全体STTを優先し、最大3回に限定する。
        capture.preview_attempts += 1
        capture.preview_last_signal_samples = signal_samples
        self._transcription_active = True
        if self._measurement is not None:
            suffix = "" if capture.preview_attempts == 1 else f"_attempt_{capture.preview_attempts}"
            for name, value in {
                "stt_preview_raw_samples": len(capture.pcm) // PCM_SAMPLE_WIDTH_BYTES,
                "stt_preview_signal_span_samples": signal_samples,
            }.items():
                self._measurement.record_utterance_event(
                    utterance_id=capture.utterance_id, name=name + suffix, stage="stt_preview", value=value,
                )
        self._schedule(self._preview_user_turn(
            utterance_id=capture.utterance_id,
            interrupted_response_id=capture.interrupted_response_id,
            microphone_pcm=bytes(capture.pcm), capture=capture,
            epoch=self._transcription_epoch,
        ))

    def _consider_stt_preparation(self, capture: _UserAudioCapture, pcm: bytes) -> None:
        if (capture.preparation_started or capture.finalized or capture.capacity_exceeded
                or capture.interrupted_response_id is not None or self._transcription_active
                or not getattr(self._session, "accepting_input", True)
                or not callable(getattr(self._session, "prepare_transcription", None))):
            return
        if len(pcm) % PCM_SAMPLE_WIDTH_BYTES:
            capture.quiet_samples = 0
            return
        # 先頭trimと同じ静音閾値。静音が続かない環境では通常STTへ任せる。
        for (sample,) in struct.iter_unpack("<h", pcm):
            capture.quiet_samples = capture.quiet_samples + 1 if abs(sample) <= 16 else 0
        if capture.quiet_samples >= STT_PREPARATION_QUIET_SAMPLES:
            capture.preparation_started = True
            self._schedule(self._prepare_transcription(capture))

    async def _prepare_transcription(self, capture: _UserAudioCapture) -> None:
        # scheduleと実行の間に発話確定・切断された場合も、新規要求を増やさない。
        if (capture.finalized or capture.capacity_exceeded or self._transcription_active
                or not getattr(self._session, "accepting_input", True)
                or not any(item is capture for item in self._user_audio_captures)):
            return
        if self._measurement is not None:
            self._measurement.record_utterance_event(
                utterance_id=capture.utterance_id, name="stt_preparation_started",
                stage="stt_preparation",
            )
        completed = False
        try:
            completed = await self._session.prepare_transcription()
        except asyncio.CancelledError:
            if self._measurement is not None:
                self._measurement.record_utterance_event(
                    utterance_id=capture.utterance_id, name="stt_preparation_await_cancelled",
                    stage="stt_preparation",
                )
            raise
        except Exception:
            # 任意の最適化の失敗を通常認識の失敗件数へ混ぜない。
            pass
        if self._measurement is not None:
            self._measurement.record_utterance_event(
                utterance_id=capture.utterance_id, name="stt_preparation_finished",
                stage="stt_preparation", value=int(completed),
            )

    async def _preview_user_turn(
        self,
        *,
        utterance_id: str,
        interrupted_response_id: str,
        microphone_pcm: bytes,
        capture: _UserAudioCapture | None = None,
        epoch: int | None = None,
    ) -> None:
        if epoch is None:
            epoch = self._transcription_epoch
        attempt = capture.preview_attempts if capture is not None else 1
        try:
            if epoch != self._transcription_epoch or not getattr(self._session, "accepting_input", True):
                return
            if self._measurement is not None:
                self._measurement.record_utterance_event(
                    utterance_id=utterance_id, name=f"stt_preview_attempt_{attempt}_started", stage="stt_preview",
                )
            prepared_audio, removed_samples = prepare_stt_audio(microphone_pcm)
            if self._measurement is not None:
                for name, value in stt_preparation_statistics(microphone_pcm, prepared_audio, removed_samples).items():
                    self._measurement.record_utterance_event(
                        utterance_id=utterance_id, name=f'stt_preview_attempt_{attempt}_' + name.removeprefix('stt_'),
                        stage='stt_preview', value=value,
                    )
            decision = await self._session.preview_turn(
                utterance_id=utterance_id,
                audio=prepared_audio,
                interrupted_response_id=interrupted_response_id,
                input_is_current=lambda: epoch == self._transcription_epoch and not self._audio_input_suppressed and (
                    capture is None or any(item is capture for item in self._user_audio_captures)
                ),
            )
            if capture is not None:
                capture.preview_complete = decision == "take_turn"
            if self._measurement is not None:
                self._measurement.record_utterance_event(
                    utterance_id=utterance_id, name=f"stt_preview_attempt_{attempt}_completed", stage="stt_preview",
                )
        except asyncio.CancelledError:
            if capture is not None:
                capture.preview_complete = True
            raise
        except Exception:
            # 先行認識の失敗時も発話全体のSTTで確定できる。
            logger.exception("Turn preview failed: utterance_id=%s", utterance_id)
        finally:
            if epoch == self._transcription_epoch:
                self._transcription_active = False
                self._start_next_transcription()
                if capture is not None:
                    self._consider_turn_preview(capture)

    async def _finalize_user_audio_if_ready(self) -> None:
        while self._user_audio_captures:
            capture = self._user_audio_captures[0]
            if not capture.finalized:
                return
            if not capture.pcm:
                if not any(item.pcm for item in tuple(self._user_audio_captures)[1:]):
                    return
                self._user_audio_captures.popleft()
                continue
            # 各発話の欠落確認が成功するまでSTTへ渡さない。
            # 音声のない旧captureは従来どおり後続を妨げず取り除く。
            if not capture.integrity_verified:
                return
            utterance_id = capture.utterance_id
            microphone_pcm = bytes(capture.pcm)
            self._user_audio_captures.popleft()
            if capture.capacity_exceeded:
                await self._session.discard_utterance(
                    utterance_id=utterance_id,
                    reason="input_capacity_exceeded",
                )
                continue
            if self._measurement is not None:
                statistics = capture.received_span.statistics(microphone_pcm)
                valid_media = capture.media_end_anchor_valid and statistics["stt_capture_received_span_valid"] == 1
                statistics["stt_capture_media_span_valid"] = 0
                if valid_media and capture.media_offset_samples is not None:
                    media_start = statistics["stt_capture_received_start_sample"] + capture.media_offset_samples
                    media_end = statistics["stt_capture_received_end_sample"] + capture.media_offset_samples
                    if (0 <= media_start <= media_end and media_end == capture.vad_detected_end_sample
                            and capture.vad_started_sample is not None
                            and capture.vad_active_end_sample is not None):
                        statistics.update(
                            stt_capture_media_span_valid=1,
                            stt_capture_media_start_sample=media_start,
                            stt_capture_media_end_sample=media_end,
                            vad_started_sample=capture.vad_started_sample,
                            vad_active_end_sample=capture.vad_active_end_sample,
                            vad_detected_end_sample=media_end,
                        )
                for name, value in statistics.items():
                    self._measurement.record_utterance_event(
                        utterance_id=utterance_id, name=name, stage='stt_capture', value=value,
                    )
            await self._enqueue_user_audio(
                utterance_id=utterance_id,
                microphone_pcm=microphone_pcm,
                interrupted_response_id=capture.interrupted_response_id,
            )

    async def _receive(self, event: dict[str, object]) -> None:
        event_type = event["type"]
        if event_type == "playback_started":
            response_id = event.get("response_id")
            if isinstance(response_id, str):
                try:
                    response = self._session.response(response_id)
                except KeyError:
                    return
                if response.state in {ResponseState.IN_PROGRESS, ResponseState.COMPLETED}:
                    self._playback_response_id = response_id
        elif event_type == "response_cancel_requested":
            response_id = event.get("response_id")
            reason = event.get("reason")
            if not isinstance(response_id, str) or not isinstance(reason, str):
                self._log_invalid_control_event(event_type)
                return
            await self._session.cancel_response(
                response_id=response_id,
                reason=reason,
            )
        elif event_type in ("playback_completed", "playback_stopped"):
            response_id = event.get("response_id")
            last_played_audio_sequence = event.get("last_played_audio_sequence")
            if (
                not isinstance(response_id, str)
                or type(last_played_audio_sequence) is not int
            ):
                self._log_invalid_control_event(event_type)
                return
            if self._playback_response_id == response_id:
                self._playback_response_id = None
            if event_type == "playback_stopped":
                # prefix検証や永続化が失敗しても、旧音声をlocal graph再接続後へ残さない。
                self._stop_audio(response_id)
            await self._session.confirm_playback(
                response_id=response_id,
                last_played_audio_sequence=last_played_audio_sequence,
            )
            if event_type == "playback_completed" and event.get("response_finished") is True:
                accepted = self._confirm_response_playback(response_id, last_played_audio_sequence)
                if accepted and self._measurement is not None and "playback_summary" in event:
                    recorded = self._measurement.record_playback_summary(
                        response_id=response_id, summary=event["playback_summary"],
                    )
                    if recorded and self._session_metrics is not None:
                        self._session_metrics.completed_playback(response_id)
        elif event_type == "session_disconnected":
            await self._session.disconnect()
        elif event_type == "session_reconnected":
            await self._session.reconnect()

    async def _enqueue_user_audio(
        self,
        *,
        utterance_id: str,
        microphone_pcm: bytes,
        interrupted_response_id: str | None = None,
    ) -> None:
        if not getattr(self._session, "accepting_input", True):
            return
        if self._transcription_active:
            if (
                len(self._pending_transcriptions) >= STT_MAX_PENDING_UTTERANCES
                or self._pending_transcription_bytes + len(microphone_pcm)
                > STT_MAX_PENDING_PCM_BYTES
            ):
                await self._session.discard_utterance(
                    utterance_id=utterance_id,
                    reason="input_capacity_exceeded",
                )
                return
            self._pending_transcriptions.append(
                (utterance_id, microphone_pcm, interrupted_response_id)
            )
            self._pending_transcription_bytes += len(microphone_pcm)
            return
        self._start_user_transcription(
            utterance_id, microphone_pcm, interrupted_response_id
        )

    def _start_user_transcription(
        self,
        utterance_id: str,
        microphone_pcm: bytes,
        interrupted_response_id: str | None = None,
    ) -> None:
        original_pcm = microphone_pcm
        microphone_pcm, trimmed_samples = prepare_stt_audio(original_pcm)
        self._transcription_active = True
        if self._measurement is not None:
            # 本文・波形は残さず、STTへ渡したPCMの長さと振幅だけを確認する。
            samples = [value[0] for value in struct.iter_unpack("<h", microphone_pcm)]
            statistics = {
                **stt_preparation_statistics(original_pcm, microphone_pcm, trimmed_samples),
                "stt_input_sample_count": len(samples),
                "stt_preroll_trimmed_samples": trimmed_samples,
                "stt_input_peak_pcm16": max((abs(value) for value in samples), default=0),
                "stt_input_rms_pcm16": (sum(value * value for value in samples) / max(1, len(samples))) ** .5,
                "stt_input_active_samples": sum(abs(value) > 200 for value in samples),
                "stt_input_first_nonzero_sample": next((i for i, value in enumerate(samples) if value != 0), len(samples)),
                "stt_input_first_above_16_sample": next((i for i, value in enumerate(samples) if abs(value) > 16), len(samples)),
                "stt_input_first_active_sample": next((i for i, value in enumerate(samples) if abs(value) > 200), len(samples)),
                "stt_input_last_active_sample": next((len(samples) - 1 - i for i, value in enumerate(reversed(samples)) if abs(value) > 200), len(samples)),
            }
            for name, value in statistics.items():
                self._measurement.record_utterance_event(
                    utterance_id=utterance_id, name=name, stage="stt", value=value,
                )
        try:
            if interrupted_response_id is None:
                task = self._session.start_transcription(
                    utterance_id=utterance_id,
                    audio=microphone_pcm,
                    should_response=True,
                )
            else:
                task = self._session.start_transcription(
                    utterance_id=utterance_id,
                    audio=microphone_pcm,
                    should_response=True,
                    interrupted_response_id=interrupted_response_id,
                )
        except RuntimeError:
            # disconnect/endとmedia tail確定の競合では、旧発話を次世代へ持ち越さない。
            if not getattr(self._session, "accepting_input", True):
                self._transcription_active = False
                return
            raise
        epoch = self._transcription_epoch
        task.add_done_callback(lambda task: self._transcription_done(task, epoch))

    def _transcription_done(self, task: asyncio.Task[object], epoch: int) -> None:
        self._consume_task(task)
        if epoch != self._transcription_epoch:
            return
        self._transcription_active = False
        self._start_next_transcription()

    def _start_next_transcription(self) -> None:
        if self._transcription_active:
            return
        if not getattr(self._session, "accepting_input", True):
            self._pending_transcriptions.clear()
            self._pending_transcription_bytes = 0
            return
        if not self._pending_transcriptions:
            return
        utterance_id, microphone_pcm, interrupted_response_id = (
            self._pending_transcriptions.popleft()
        )
        self._pending_transcription_bytes -= len(microphone_pcm)
        self._start_user_transcription(
            utterance_id, microphone_pcm, interrupted_response_id
        )

    @staticmethod
    def _is_user_event(event: dict[str, object]) -> bool:
        speaker = event.get("speaker")
        return isinstance(speaker, dict) and speaker.get("role") == "user"

    async def end(self) -> None:
        await self.close_audio()
        await self._session.end()

    @staticmethod
    def _log_invalid_control_event(event_type: object) -> None:
        logger.warning(
            "Invalid Conversation Core control event discarded: type=%s",
            event_type,
        )

    @staticmethod
    def _consume_task(task: asyncio.Task[object]) -> None:
        if not task.cancelled():
            task.exception()


@dataclass
class _SessionCleanupState:
    room: rtc.Room | None
    pending_runtime_tasks: list[asyncio.Task[None]]
    audio_source: ResponseAudioTracks | None = None
    session_binding_deleted: bool = False
    room_deleted: bool = False
    room_cleanup_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ProductionRuntimeManager:
    manages_owned_cleanup: bool = True

    def __init__(
        self,
        *,
        livekit_url: str,
        signer: ProductionTokenSigner,
        room_manager: ProductionRoomManager,
        session_repository: InMemorySessionBindingRepository,
        core_port: CoreNotificationPort,
        core_session_factory: _CoreSessionFactory | None = None,
        screen_session_revoker: _ScreenSessionRevoker | None = None,
        audio_probe_enabled: bool = False,
        session_trace_recorder: JsonlTraceRecorder | None = None,
        measurement_kind: MeasurementKind = "automated_test",
    ) -> None:
        self._session_trace_recorder = session_trace_recorder
        self._measurement_kind = measurement_kind
        self._audio_probe_enabled = audio_probe_enabled
        self._livekit_url = livekit_url
        self._signer = signer
        self._room_manager = room_manager
        self._sessions = session_repository
        self._core_port = core_port
        self._core_session_factory = (
            core_session_factory
            if core_session_factory is not None
            else _MissingCoreSessionFactory()
        )
        self._screen_session_revoker = screen_session_revoker
        self._rooms: dict[str, rtc.Room] = {}
        self._coordinators: dict[str, ProductionSessionCoordinator] = {}
        self._session_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._participant_event_tails: dict[str, asyncio.Task[None]] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._audio_sources: dict[str, ResponseAudioTracks] = {}
        self._core_sessions: dict[str, ConversationCoreSession] = {}
        self._core_bridges: dict[str, _ConversationCoreBridge] = {}
        self._microphone_integrities: dict[tuple[str, str], MicrophoneIntegrity] = {}
        self._cleanup_states: dict[str, _SessionCleanupState] = {}
        self._screen_client_sessions: dict[str, UUID] = {}

    async def connect(self, session_id: str) -> None:
        reservation = self._sessions.get(session_id)
        if reservation is None:
            raise RuntimeError("session reservation is required")
        await self.start_runtime(
            {
                "session_id": session_id,
                "identity": f"character-{reservation.request['character_id']}-{session_id}",
                "core_participant_id": str(reservation.request.get("participant_id", session_id)),
                "character_id": str(reservation.request["character_id"]),
                "conversation_id": str(reservation.request["conversation_id"]),
                "reconnect_grace_ms": min(
                    _required_int(
                        reservation.request["requested_reconnect_grace_ms"],
                        "requested_reconnect_grace_ms",
                    ),
                    60_000,
                ),
                **(
                    {}
                    if reservation.request.get("screen_client_session_id") is None
                    else {
                        "screen_client_session_id": reservation.request[
                            "screen_client_session_id"
                        ]
                    }
                ),
            }
        )

    async def wait_until_ready(self, session_id: str) -> None:
        await self._ready[session_id].wait()

    async def start_runtime(self, request: dict[str, object]) -> None:
        rtc_module = _livekit_rtc_module()

        session_id = str(request["session_id"])
        if request.get("screen_client_session_id") is not None:
            self._screen_client_sessions[session_id] = UUID(
                str(request["screen_client_session_id"])
            )
        room_name = f"voice-{session_id}"
        user_identity = f"user-{session_id}"
        async with preparation_operation("token", BOOTSTRAP_TIMEOUT_SECONDS):
            token = await self._signer.issue_token(
                {
                    "identity": request["identity"],
                    "room": room_name,
                    "ttl_seconds": 90,
                    "can_subscribe": True,
                    "can_publish": True,
                    "can_publish_data": True,
                    "can_publish_sources": ["microphone"],
                }
            )
        room: rtc.Room = rtc_module.Room()

        if self._audio_probe_enabled:
            room_observation_count = 0

            def observe_room_stage(stage: str) -> None:
                nonlocal room_observation_count
                if room_observation_count > 128:
                    return
                if room_observation_count == 128:
                    stage = "overflow"
                room_observation_count += 1
                # SDKの接続先・identity・例外本文は診断へ渡さない。
                logger.warning("RTC lifecycle: stage=%s at_ms=%d", stage, time.monotonic_ns() // 1_000_000)

            room.on("connected")(lambda: observe_room_stage("connected"))
            room.on("reconnecting")(lambda: observe_room_stage("reconnecting"))
            room.on("reconnected")(lambda: observe_room_stage("reconnected"))
            room.on("disconnected")(lambda _reason: observe_room_stage("disconnected"))

        async def publish_data(payload: bytes, topic: str) -> None:
            await room.local_participant.publish_data(payload, reliable=True, topic=topic)

        async def cleanup(_owned_session_id: str) -> None:
            await self._cleanup_owned_session(session_id)

        # 同じmicrophone trackでも、世代が変わると旧readerは入力を拒否して終了する。
        # readerの終了を待ってから置き換え、二重取込と購読解除後の復活を防ぐ。
        microphone_readers: dict[
            int, tuple[rtc.Track, str, str, int, asyncio.Task[None] | None]
        ] = {}
        microphone_lock = asyncio.Lock()
        microphone_changed = asyncio.Event()

        async def replace_microphone_reader(
            track: rtc.Track, identity: str, participant_sid: str,
        ) -> None:
            key = id(track)
            old = microphone_readers.get(key)
            if (old is not None and old[1] == identity and old[2] == participant_sid
                    and old[3] == coordinator.generation
                    and old[4] is not None and not old[4].done()
                    and coordinator.is_current_participant(identity=identity, participant_sid=participant_sid)):
                return
            microphone_readers.pop(key, None)
            if old is not None and old[4] is not None:
                old[4].cancel()
                await asyncio.gather(old[4], return_exceptions=True)
            if not coordinator.is_current_participant(identity=identity, participant_sid=participant_sid):
                return
            generation = coordinator.generation
            task = self._schedule_task(session_id, self._observe_microphone(
                session_id, track, coordinator, identity, participant_sid, generation, publish_data,
            ))
            if task is not None:
                microphone_readers[key] = (track, identity, participant_sid, generation, task)
                microphone_changed.set()

        async def authorize_microphone(track_sid: str) -> bool:
            try:
                # FEの5秒ACK待ちに収める。統計開始前の発話を認可し、後から欠測で
                # 破棄する状態を避けるため、無音trackで最初の有効統計を待つ。
                async with asyncio.timeout(4):
                    while True:
                        for track, identity, participant_sid, generation, task in tuple(microphone_readers.values()):
                            if (str(track.sid) == track_sid and task is not None and not task.done()
                                    and generation == coordinator.generation
                                    and coordinator.is_current_participant(identity=identity, participant_sid=participant_sid)):
                                monitor = self._microphone_integrities.get((session_id, track_sid))
                                if monitor is None:
                                    # reader taskは登録直後、最初の実行をまだ待っている場合がある。
                                    await asyncio.sleep(0.01)
                                    break
                                await monitor.wait_ready()
                                return (not task.done() and generation == coordinator.generation
                                        and self._microphone_integrities.get((session_id, track_sid)) is monitor
                                        and coordinator.is_current_participant(
                                            identity=identity, participant_sid=participant_sid))
                        else:
                            microphone_changed.clear()
                            await microphone_changed.wait()
            except (TimeoutError, AudioInputFault):
                return False

        async def verify_audio_integrity(track_sid: str, start_sample: int, end_sample: int) -> None:
            monitor = self._microphone_integrities.get((session_id, track_sid))
            if monitor is None:
                raise AudioInputFault("audio_integrity_unavailable")
            await monitor.verify(start_sample, end_sample)

        async def generation_ready() -> None:
            if audio_probe is not None:
                await audio_probe.cancel()
            async with microphone_lock:
                for track, identity, participant_sid, _generation, _task in tuple(microphone_readers.values()):
                    await replace_microphone_reader(track, identity, participant_sid)

        def response_track_ready(response_id: str, track_sid: str) -> None:
            source = self._audio_sources.get(session_id)
            if source is not None:
                source.confirm_ready(response_id, track_sid)

        from app.livekit_transport.audio_probe import AudioProbePublisher
        audio_probe = AudioProbePublisher(room,
            current=lambda generation: coordinator.phase == "available" and coordinator.generation == generation,
            publish=lambda frame: publish_data(json.dumps(frame).encode(), PRIVATE_TOPIC),
            schedule=lambda operation: self._schedule_task(session_id, operation),
        ) if self._audio_probe_enabled else None

        def handle_audio_probe(kind: str, probe_id: str, generation: int, sid: str | None) -> None:
            if audio_probe is None:
                return
            if kind == "audio_probe_request":
                audio_probe.request(probe_id, generation)
            elif sid is not None:
                audio_probe.receive(kind, probe_id, generation, sid)

        sync_observation_count = 0

        def observe_sync(stage: str, generation: int, at_ms: int) -> None:
            nonlocal sync_observation_count
            if sync_observation_count > 512:
                return
            if sync_observation_count == 512:
                stage = "overflow"
            sync_observation_count += 1
            # 専用test Profileのみ。本文、ID、接続先、例外文字列を含めない。
            logger.warning("State sync: stage=%s generation=%d at_ms=%d", stage, generation, at_ms)

        session_metrics = (
            SessionMetrics(character_id=str(request["character_id"]), session_id=session_id,
                           measurement_kind=self._measurement_kind,
                           record=self._session_trace_recorder.record_session)
            if self._session_trace_recorder is not None else None
        )
        coordinator = ProductionSessionCoordinator(
            session_id=session_id,
            user_identity=user_identity,
            core_participant_id=str(request["core_participant_id"]),
            reconnect_grace_ms=_required_int(
                request["reconnect_grace_ms"], "reconnect_grace_ms"
            ),
            dependencies=SessionCoordinatorDependencies(
                publish_data=publish_data,
                cleanup=cleanup,
                generation_ready=generation_ready,
                response_track_ready=response_track_ready,
                audio_probe=handle_audio_probe if audio_probe is not None else None,
                sync_observer=observe_sync if self._audio_probe_enabled else None,
                session_metrics=session_metrics,
            ),
            core_port=self._core_port,
        )
        self._rooms[session_id] = room
        self._coordinators[session_id] = coordinator
        self._session_tasks[session_id] = set()
        self._ready[session_id] = asyncio.Event()
        rtc_diagnostic = None
        if self._audio_probe_enabled:
            from app.livekit_transport.rtc_diagnostic import RtcIngressDiagnostic
            rtc_diagnostic = RtcIngressDiagnostic(lambda: coordinator.generation)
            stats_task: asyncio.Task[None] | None = None

            def start_rtc_diagnostic() -> None:
                nonlocal stats_task
                if stats_task is None:
                    stats_task = self._schedule_task(session_id, rtc_diagnostic.sample(room))

            room.on("reconnecting")(start_rtc_diagnostic)

        def participant_connected(participant: rtc.RemoteParticipant) -> None:
            async def handle_connected() -> None:
                room_sid_value = room.sid
                room_sid = (
                    str(await room_sid_value)
                    if inspect.isawaitable(room_sid_value)
                    else str(room_sid_value)
                )
                reconnected = coordinator.participant_connected(
                    identity=str(participant.identity),
                    participant_sid=str(participant.sid),
                    room_sid=room_sid,
                )
                if reconnected:
                    await coordinator.synchronize_reconnection()

            self._schedule_serialized_participant_operation(
                session_id, handle_connected
            )
        room.on("participant_connected")(participant_connected)

        def participant_disconnected(participant: rtc.RemoteParticipant) -> None:
            async def handle_disconnected() -> None:
                coordinator.participant_disconnected(
                    identity=str(participant.identity),
                    participant_sid=str(participant.sid),
                )

            self._schedule_serialized_participant_operation(
                session_id, handle_disconnected
            )

        room.on("participant_disconnected")(participant_disconnected)

        def data_received(packet: rtc.DataPacket) -> None:
            participant = getattr(packet, "participant", None)
            if rtc_diagnostic is not None:
                participant_kind = "missing" if participant is None else "current" if coordinator.is_current_participant(
                    identity=str(participant.identity), participant_sid=str(participant.sid)) else "other"
                rtc_diagnostic.ingress({PRIVATE_TOPIC: "private", APPLICATION_TOPIC: "application", SCREEN_TOPIC: "screen"}.get(
                    str(packet.topic), "other"), participant_kind)
            if participant is None:
                return
            self._schedule_task(
                session_id,
                coordinator.receive_data(
                    identity=str(participant.identity),
                    participant_sid=str(participant.sid),
                    topic=str(packet.topic),
                    payload=bytes(packet.data),
                ),
            )

        room.on("data_received")(data_received)

        def track_subscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            async def handle_track_subscribed() -> None:
                if (
                    publication.source
                    != rtc_module.TrackSource.SOURCE_MICROPHONE
                    or not coordinator.is_current_participant(
                        identity=str(participant.identity),
                        participant_sid=str(participant.sid),
                    )
                ):
                    return
                async with microphone_lock:
                    await replace_microphone_reader(track, str(participant.identity), str(participant.sid))

            self._schedule_serialized_participant_operation(
                session_id, handle_track_subscribed
            )

        room.on("track_subscribed")(track_subscribed)

        def track_unsubscribed(
            track: rtc.Track,
            _publication: rtc.RemoteTrackPublication,
            _participant: rtc.RemoteParticipant,
        ) -> None:
            async def handle_track_unsubscribed() -> None:
                async with microphone_lock:
                    old = microphone_readers.pop(id(track), None)
                    microphone_changed.set()
                    if old is not None and old[4] is not None:
                        old[4].cancel()
                        await asyncio.gather(old[4], return_exceptions=True)

            self._schedule_serialized_participant_operation(session_id, handle_track_unsubscribed)

        room.on("track_unsubscribed")(track_unsubscribed)

        def startup_ended() -> bool:
            return (
                self._coordinators.get(session_id) is not coordinator
                or coordinator.phase == "ended"
            )

        async with preparation_operation("transport", BOOTSTRAP_TIMEOUT_SECONDS):
            await room.connect(self._livekit_url, token)
        if startup_ended():
            raise RuntimeError("runtime startup ended")
        async with preparation_operation("output", BOOTSTRAP_TIMEOUT_SECONDS):
            audio_source = await self._prepare_output_track(room)
        if startup_ended():
            await audio_source.aclose()
            raise RuntimeError("runtime startup ended")
        self._audio_sources[session_id] = audio_source
        delivery = _ConversationCoreDelivery(
            coordinator=coordinator,
            audio_source=audio_source,
            character_participant_id=str(uuid4()),
            character_id=str(request["character_id"]),
            user_participant_id=str(request["core_participant_id"]),
        )
        create_session = getattr(
            self._core_session_factory, "create_ready", self._core_session_factory.create,
        )
        created_session = create_session(
            session_id=session_id,
            character_id=str(request["character_id"]),
            conversation_id=UUID(str(request["conversation_id"])),
            delivery=delivery,
            client_session_id=(
                UUID(str(request["screen_client_session_id"]))
                if request.get("screen_client_session_id") is not None
                else None
            ),
        )

        core_session = (
            await created_session if inspect.isawaitable(created_session) else created_session
        )
        # await中の期限切れ・終了後は、遅れて完成したCoreを登録せず閉じる。
        if startup_ended():
            await core_session.end()
            raise RuntimeError("runtime startup ended")

        def schedule_core_operation(operation: Awaitable[None]) -> None:
            self._schedule_task(session_id, operation)

        async def submit_text(input_id: str, text: str) -> str | None:
            discarded = bridge.invalidate_unfinalized_audio()
            response = await core_session.submit_text(
                input_id=input_id, text=text, discard_speech_ids=discarded,
            )
            return response.response_id if response is not None else None

        async def publish_input_result(event: dict[str, object]) -> None:
            await coordinator.send_core(json.dumps(event, ensure_ascii=False).encode())

        bridge = _ConversationCoreBridge(
            core_session,
            schedule_core_operation,
            stop_audio=audio_source.clear,
            confirm_response_playback=delivery.confirm_response_playback,
            measurement=delivery.measurement,
            session_metrics=session_metrics,
            publish_audio_event=publish_input_result,
            authorize_microphone=authorize_microphone,
            verify_audio_integrity=verify_audio_integrity,
            user_participant_id=str(request["core_participant_id"]),
            text_input=TextInputReceiver(
                session_id=session_id, participant_id=str(request["core_participant_id"]),
                submit=submit_text, publish=publish_input_result,
                accepting_input=lambda: core_session.accepting_input,
            ),
        )
        self._core_sessions[session_id] = core_session
        self._core_bridges[session_id] = bridge
        await bridge.prepare_audio()
        if isinstance(self._core_port, ProductionCoreEventInbox):
            self._core_port.bind(session_id, bridge.notify)
        if startup_ended():
            raise RuntimeError("runtime startup ended")
        # clientのjoin猶予をモデル準備で消費しない。
        coordinator.start_join_deadline()
        self._ready[session_id].set()

    async def _observe_microphone(
        self,
        session_id: str,
        track: rtc.Track,
        coordinator: ProductionSessionCoordinator,
        participant_identity: str,
        participant_sid: str,
        generation: int,
        publish_data: Callable[[bytes, str], Awaitable[None]],
    ) -> None:
        rtc_module = _livekit_rtc_module()

        def schedule_observation(operation: Awaitable[None]) -> None:
            self._schedule_task(session_id, operation)

        observer = MicrophoneTrackObserver(
            observation_port=_ObservationPublisher(
                publish_data,
                lambda: coordinator.generation,
                schedule_observation,
            ),
            sample_rate=STT_SAMPLE_RATE,
        )
        clock = MicrophoneFrameClock()
        stream: rtc.AudioStream = rtc_module.AudioStream(
            track, sample_rate=STT_SAMPLE_RATE, num_channels=1,
            capacity=MICROPHONE_QUEUE_CAPACITY, frame_size_ms=MICROPHONE_FRAME_MS,
            noise_cancellation=clock,
        )
        key = (session_id, str(track.sid))
        monitor = MicrophoneIntegrity(track.get_stats, lambda: clock.samples_seen)
        self._microphone_integrities[key] = monitor
        monitor_task = asyncio.create_task(monitor.run())
        try:
            async for event in stream:
                if (
                    coordinator.generation != generation
                    or not coordinator.is_current_participant(
                        identity=participant_identity,
                        participant_sid=participant_sid,
                    )
                ):
                    return
                frame = event.frame
                bridge = self._core_bridges.get(session_id)
                if bridge is None:
                    return
                try:
                    pcm, start_sample = clock.read(frame)
                except AudioInputFault:
                    return
                await bridge.receive_microphone_frame(
                    pcm, start_sample=start_sample, track_sid=str(track.sid),
                )
                observer.receive_frame(
                    pcm=pcm,
                    sample_count=int(frame.samples_per_channel),
                    received_at_ms=int(time.monotonic() * 1000),
                )
                if session_id not in self._rooms:
                    return
        except Exception:
            # 端末trackの読取失敗はfinallyで入力停止として通知する。
            # text・終了済み発話・回答再生はこのreaderの所有物ではない。
            logger.warning("LiveKit microphone reader failed")
        finally:
            monitor.close()
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
            if self._microphone_integrities.get(key) is monitor:
                self._microphone_integrities.pop(key, None)
            bridge = self._core_bridges.get(session_id)
            if bridge is not None:
                bridge.close_microphone_track(str(track.sid))
            await stream.aclose()

    def _schedule_task(
        self, session_id: str, coroutine: Awaitable[None]
    ) -> asyncio.Task[None] | None:
        tasks = self._session_tasks.get(session_id)
        if tasks is None:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            elif isinstance(coroutine, asyncio.Future):
                coroutine.cancel()
            return None
        task = asyncio.create_task(self._run_owned_task(session_id, coroutine))
        tasks.add(task)
        task.add_done_callback(lambda completed: self._task_done(tasks, completed))
        return task

    def _schedule_serialized_participant_operation(
        self,
        session_id: str,
        operation: Callable[[], Awaitable[None]],
    ) -> None:
        previous = self._participant_event_tails.get(session_id)

        async def run_serialized() -> None:
            if previous is not None:
                await previous
            await operation()

        task = self._schedule_task(session_id, run_serialized())
        if task is None:
            return
        self._participant_event_tails[session_id] = task
        task.add_done_callback(
            lambda completed: self._participant_operation_done(
                session_id, completed
            )
        )

    def _participant_operation_done(
        self, session_id: str, completed: asyncio.Task[None]
    ) -> None:
        if self._participant_event_tails.get(session_id) is completed:
            self._participant_event_tails.pop(session_id, None)

    async def _run_owned_task(
        self, session_id: str, operation: Awaitable[None]
    ) -> None:
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception:
            coordinator = self._coordinators.get(session_id)
            if coordinator is not None and coordinator.phase != "ended":
                await coordinator.cleanup("runtime_error")
            raise

    @staticmethod
    def _task_done(
        tasks: set[asyncio.Task[None]], completed: asyncio.Task[None]
    ) -> None:
        tasks.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "LiveKit session task failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _prepare_output_track(self, room: rtc.Room) -> ResponseAudioTracks:
        # 応答決定まで無所属の出力trackを発行しない。
        return ResponseAudioTracks(room, sample_rate=PCM_SAMPLE_RATE, channels=PCM_CHANNELS)

    async def send_core(self, session_id: str, payload: bytes) -> None:
        coordinator = self._coordinators.get(session_id)
        if coordinator is None:
            raise RuntimeError("LiveKit session is not active")
        await coordinator.send_core(payload)

    async def send_screen(self, session_id: str, payload: bytes) -> None:
        coordinator = self._coordinators.get(session_id)
        if coordinator is None:
            raise RuntimeError("LiveKit session is not active")
        await coordinator.send_screen(payload)

    def confirmation_session(self, character: str, conversation: str) -> str | None:
        """管理画面の選択中スレッドによらず、要求を所有する音声sessionへ続行する。"""
        for session_id in self._core_sessions:
            reservation = self._sessions.get(session_id)
            if (
                reservation is not None
                and str(reservation.request["character_id"]) == character
                and str(reservation.request["conversation_id"]) == conversation
            ):
                return session_id
        return None

    async def submit_action_confirmation(
        self, session_id: str, character: str, conversation: str,
        request_id: str, message: str, still_waiting: Callable[[], bool],
    ) -> bool:
        """画面操作から音声応答を開始する。STT入力や終了済み活動を再開しない。"""
        reservation = self._sessions.get(session_id)
        core = self._core_sessions.get(session_id)
        if (
            reservation is None or core is None
            or str(reservation.request["character_id"]) != character
            or str(reservation.request["conversation_id"]) != conversation
        ):
            raise ValueError("voice_confirmation_session_mismatch")
        # 確認の読み上げが終わるのを待ち、既存応答と入力を混ぜない。
        async with asyncio.timeout(30):
            while core.active_response is not None:
                if not core.accepting_input or not still_waiting():
                    return False
                await asyncio.sleep(0.05)
        if not core.accepting_input or not still_waiting():
            return False
        response = await core.finalize_utterance(
            utterance_id=request_id, transcript=message, should_response=True,
            control_request_id=request_id,
            control_input_valid=still_waiting,
        )
        return response is not None

    async def stop(self, session_id: str) -> None:
        coordinator = self._coordinators.get(session_id)
        if coordinator is not None:
            await coordinator.cleanup("explicit")
        else:
            await self._cleanup_owned_session(session_id)

    async def stop_all(self) -> None:
        session_ids = set(self._rooms) | set(self._cleanup_states)
        for session_id in session_ids:
            await self.stop(session_id)

    def _release_runtime_ownership(
        self, session_id: str
    ) -> tuple[rtc.Room | None, list[asyncio.Task[None]]]:
        self._coordinators.pop(session_id, None)
        self._participant_event_tails.pop(session_id, None)
        tasks = self._session_tasks.pop(session_id, set())
        pending_tasks: list[asyncio.Task[None]] = []
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
                pending_tasks.append(task)
        room = self._rooms.pop(session_id, None)
        self._ready.pop(session_id, None)
        self._audio_sources.pop(session_id, None)
        self._core_bridges.pop(session_id, None)
        if isinstance(self._core_port, ProductionCoreEventInbox):
            self._core_port.unbind(session_id)
        return room, pending_tasks

    async def _cleanup_owned_session(self, session_id: str) -> None:
        state = self._cleanup_states.get(session_id)
        if state is None:
            screen_client_session_id = getattr(
                self, "_screen_client_sessions", {}
            ).pop(session_id, None)
            if screen_client_session_id is not None and self._screen_session_revoker is not None:
                await self._screen_session_revoker.revoke_client(
                    screen_client_session_id,
                    "backend_disconnect",
                )
            bridge = self._core_bridges.get(session_id)
            if bridge is not None:
                await bridge.close_audio()
            core_session = self._core_sessions.pop(session_id, None)
            if core_session is not None:
                await core_session.end()
            audio_source = self._audio_sources.get(session_id)
            room, pending_tasks = self._release_runtime_ownership(session_id)
            state = _SessionCleanupState(
                room=room,
                pending_runtime_tasks=pending_tasks,
                audio_source=audio_source,
            )
            self._cleanup_states[session_id] = state

        async with state.lock:
            room_cleanup_task = self._start_room_cleanup(session_id, state)
            local_operations: list[Awaitable[object]] = []
            if state.audio_source is not None:
                local_operations.append(asyncio.create_task(self._close_audio_source(state)))
            if not state.session_binding_deleted:
                local_operations.append(
                    asyncio.create_task(self._delete_session_binding(session_id, state))
                )
            if state.room is not None:
                local_operations.append(
                    asyncio.create_task(self._disconnect_room(state))
                )
            if state.pending_runtime_tasks:
                local_operations.append(
                    asyncio.create_task(self._finish_runtime_tasks(state))
                )

            local_results = await asyncio.gather(
                *local_operations, return_exceptions=True
            )
            room_cleanup_result: object = None
            if room_cleanup_task is not None:
                try:
                    await asyncio.shield(room_cleanup_task)
                except Exception as error:
                    room_cleanup_result = error
            for result in local_results:
                if isinstance(result, BaseException):
                    raise result
            if isinstance(room_cleanup_result, BaseException):
                raise RoomCleanupPendingError(
                    str(room_cleanup_result)
                ) from room_cleanup_result
            if self._cleanup_states.get(session_id) is state:
                self._cleanup_states.pop(session_id)

    @staticmethod
    async def _close_audio_source(state: _SessionCleanupState) -> None:
        if state.audio_source is not None:
            await state.audio_source.aclose()
            state.audio_source = None

    async def _delete_session_binding(
        self, session_id: str, state: _SessionCleanupState
    ) -> None:
        await self._sessions.delete(session_id)
        state.session_binding_deleted = True

    @staticmethod
    async def _disconnect_room(state: _SessionCleanupState) -> None:
        if state.room is None:
            raise RuntimeError("cleanup room is required")
        await state.room.disconnect()
        state.room = None

    @staticmethod
    async def _finish_runtime_tasks(state: _SessionCleanupState) -> None:
        await asyncio.gather(*state.pending_runtime_tasks, return_exceptions=True)
        state.pending_runtime_tasks.clear()

    def _start_room_cleanup(
        self, session_id: str, state: _SessionCleanupState
    ) -> asyncio.Task[None] | None:
        if state.room_deleted:
            return None
        existing = state.room_cleanup_task
        if existing is not None and not existing.done():
            return existing
        if existing is not None and not existing.cancelled():
            existing.exception()
        task = asyncio.create_task(self._delete_owned_room(session_id, state))
        state.room_cleanup_task = task
        task.add_done_callback(self._consume_room_cleanup_result)
        return task

    async def _delete_owned_room(
        self, session_id: str, state: _SessionCleanupState
    ) -> None:
        await self._room_manager.delete(f"voice-{session_id}")
        state.room_deleted = True

    @staticmethod
    def _consume_room_cleanup_result(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()


async def configure_production_resources(
    app: FastAPI,
    *,
    core_session_factory: _CoreSessionFactory | None,
) -> livekit_api.LiveKitAPI | None:
    from app.livekit_transport.audio_probe import audio_probe_enabled

    settings = resolve_livekit_settings()
    if settings is None:
        return None
    if core_session_factory is None:
        raise RuntimeError("Conversation Core session factory is required")
    livekit_url, api_key, api_secret = settings
    api = _livekit_api_module()

    client: livekit_api.LiveKitAPI = api.LiveKitAPI(livekit_url, api_key, api_secret)
    room_manager = ProductionRoomManager(client)
    signer = ProductionTokenSigner(api_key, api_secret)
    sessions = InMemorySessionBindingRepository()
    core_events = ProductionCoreEventInbox()
    runtime = ProductionRuntimeManager(
        livekit_url=livekit_url,
        signer=signer,
        room_manager=room_manager,
        session_repository=sessions,
        core_port=core_events,
        core_session_factory=core_session_factory,
        screen_session_revoker=app.state.screen_perception_service,
        audio_probe_enabled=audio_probe_enabled(os.environ, livekit_url),
        session_trace_recorder=getattr(app.state, "voice_trace_recorder", None),
        measurement_kind=getattr(app.state, "voice_measurement_kind", "automated_test"),
    )
    validator = CharacterConversationBindingValidator(
        character_loader=load_character_card,
        conversations=app.state.conversation_history_repository,
    )
    bootstrap = BootstrapService(
        session_repository=sessions,
        room_manager=room_manager,
        runtime_manager=runtime,
        token_signer=signer,
        timeout_seconds=BOOTSTRAP_TIMEOUT_SECONDS,
        binding_validator=validator,
    )
    app.state.livekit_room_manager = room_manager
    app.state.livekit_session_repository = sessions
    app.state.livekit_runtime_manager = runtime
    app.state.livekit_token_signer = signer
    app.state.livekit_bootstrap_service = bootstrap
    app.state.livekit_core_events = core_events
    app.state.livekit_url = livekit_url
    return client
