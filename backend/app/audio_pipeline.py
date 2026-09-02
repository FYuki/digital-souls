import os
from contextlib import contextmanager
from dataclasses import dataclass
import logging
from time import perf_counter
from typing import Callable, Iterator, Protocol
from uuid import uuid4

from app.audio.constants import PCM_SAMPLE_WIDTH_BYTES
from app.characters.loader import VoicevoxTtsConfig, load_tts_config
from app.chat_service import (
    ChatReply,
    PersistedContentTurn,
    PersistedPrivacySkippedTurn,
)
from app.model_settings import ModelSettings
from app.runtime_paths import RuntimePaths
from app.tts.speech_synthesizer import SpeechSynthesizer
from app.tts.voicevox_client import (
    DEFAULT_VOICEVOX_BASE_URL,
    VOICEVOX_BASE_URL_ENV,
    create_voicevox_client,
)
from app.voice_metrics import EventOutcome, MeasurementContext, TraceEvent

logger = logging.getLogger(__name__)
ReplyGenerator = Callable[[str], ChatReply]
CLIENT_INPUT_ERROR_STATUS = 422
UPSTREAM_SERVICE_ERROR_STATUS = 502
CAPACITY_ERROR_STATUS = 429
INFERENCE_TIMEOUT_STATUS = 504
UNREADABLE_CHARACTER_CARD_MESSAGE = "character card is not readable"


class AudioPipelineConfigError(ValueError):
    """Server-side audio pipeline configuration is invalid."""


class AudioPipelineStepError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class SpeechTranscriber(Protocol):
    def transcribe(self, audio: bytes) -> str:
        ...


@contextmanager
def _log_step_latency(step_name: str) -> Iterator[None]:
    started_at = perf_counter()
    try:
        yield
    finally:
        logger.info("%s completed in %.3fs", step_name, perf_counter() - started_at)


def _validate_pcm16_audio(audio: bytes) -> None:
    if len(audio) % PCM_SAMPLE_WIDTH_BYTES != 0:
        raise AudioPipelineStepError(
            CLIENT_INPUT_ERROR_STATUS,
            "Audio length must be a multiple of "
            f"{PCM_SAMPLE_WIDTH_BYTES} bytes, got {len(audio)}",
        )


@dataclass(frozen=True)
class AudioRuntimeConfig:
    voicevox_base_url: str
    model_settings: ModelSettings
    whisper_base_url: str


class AudioPipelineSession:
    def __init__(
        self,
        tts_config: VoicevoxTtsConfig,
        transcriber: SpeechTranscriber,
        speech_synthesizer: SpeechSynthesizer,
        measurement: MeasurementContext | None = None,
    ) -> None:
        self._tts_config = tts_config
        self._transcriber = transcriber
        self._speech_synthesizer = speech_synthesizer
        self._measurement = measurement

    def generate_response_audio(
        self,
        audio: bytes,
        reply_generator: ReplyGenerator,
        *,
        measurement: MeasurementContext | None = None,
    ) -> tuple[str, ChatReply, bytes]:
        active_measurement = measurement or self._measurement
        message = self._transcribe_audio(audio, active_measurement)
        reply = self._generate_reply(reply_generator, message, active_measurement)
        response_audio = self._response_audio(reply, active_measurement)
        return message, reply, response_audio

    def _record_event(
        self,
        measurement: MeasurementContext | None,
        *,
        name: str,
        stage: str,
        outcome: EventOutcome = "success",
        reason_code: str | None = None,
    ) -> None:
        if measurement is None:
            return
        measurement.record(
            TraceEvent(
                schema_version="1.0",
                measurement_kind=measurement.measurement_kind,
                event_id=str(uuid4()),
                character_id=measurement.character_id,
                session_id=measurement.session_id,
                utterance_id=measurement.utterance_id,
                response_id=measurement.response_id,
                name=name,
                stage=stage,
                outcome=outcome,
                reason_code=reason_code,
                timestamp=measurement.clock_ns(),
                clock_domain="server_monotonic",
                unit="nanosecond",
            )
        )

    def _response_audio(
        self,
        reply: ChatReply,
        measurement: MeasurementContext | None,
    ) -> bytes:
        if isinstance(reply.persisted_turn, PersistedPrivacySkippedTurn):
            self._record_event(
                measurement,
                name="response_excluded",
                stage="response",
                outcome="excluded",
                reason_code="privacy_skip",
            )
            return b""
        if not isinstance(reply.persisted_turn, PersistedContentTurn):
            raise TypeError("unsupported persisted turn")
        return self._synthesize_reply(
            reply.persisted_turn.assistant_content,
            measurement,
        )

    def _transcribe_audio(
        self,
        audio: bytes,
        measurement: MeasurementContext | None,
    ) -> str:
        _validate_pcm16_audio(audio)
        self._record_event(measurement, name="stt_started", stage="stt")
        with _log_step_latency("STT"):
            try:
                message = self._transcriber.transcribe(audio)
            except Exception as exc:
                reason_code = getattr(exc, "error_code", "stt_upstream_failed")
                status_code, detail = {
                    "stt_capacity_exceeded": (
                        CAPACITY_ERROR_STATUS,
                        "STT capacity exceeded",
                    ),
                    "stt_inference_timeout": (
                        INFERENCE_TIMEOUT_STATUS,
                        "STT inference timed out",
                    ),
                }.get(
                    reason_code,
                    (UPSTREAM_SERVICE_ERROR_STATUS, "STT request failed"),
                )
                self._record_event(
                    measurement,
                    name="stt_failed",
                    stage="stt",
                    outcome="failure",
                    reason_code=reason_code,
                )
                logger.exception("STT failed")
                raise AudioPipelineStepError(
                    status_code,
                    detail,
                ) from exc
        self._record_event(measurement, name="stt_completed", stage="stt")
        return message

    def _generate_reply(
        self,
        reply_generator: ReplyGenerator,
        message: str,
        measurement: MeasurementContext | None,
    ) -> ChatReply:
        self._record_event(measurement, name="llm_started", stage="llm")
        with _log_step_latency("LLM"):
            try:
                reply = reply_generator(message)
            except Exception:
                self._record_event(
                    measurement,
                    name="llm_failed",
                    stage="llm",
                    outcome="failure",
                    reason_code="llm_upstream_failed",
                )
                raise
        self._record_event(measurement, name="first_text_delta", stage="llm")
        self._record_event(measurement, name="llm_completed", stage="llm")
        return reply

    def _synthesize_reply(
        self,
        reply: str,
        measurement: MeasurementContext | None,
    ) -> bytes:
        self._record_event(measurement, name="tts_started", stage="tts")
        with _log_step_latency("VOICEVOX"):
            try:
                audio = self._speech_synthesizer.synthesize(
                    reply,
                    self._tts_config.speaker_id,
                )
            except Exception as exc:
                self._record_event(
                    measurement,
                    name="tts_failed",
                    stage="tts",
                    outcome="failure",
                    reason_code="tts_upstream_failed",
                )
                logger.exception("VOICEVOX synthesis failed")
                raise AudioPipelineStepError(
                    UPSTREAM_SERVICE_ERROR_STATUS,
                    "VOICEVOX request failed",
                ) from exc
        self._record_event(measurement, name="tts_completed", stage="tts")
        return audio


class AudioPipelineService:
    def __init__(
        self,
        transcriber: SpeechTranscriber,
        speech_synthesizer: SpeechSynthesizer,
    ) -> None:
        self._transcriber = transcriber
        self._speech_synthesizer = speech_synthesizer

    def create_session(self, character: str) -> AudioPipelineSession:
        try:
            tts_config = load_tts_config(character)
        except FileNotFoundError as exc:
            raise AudioPipelineConfigError("character card is required") from exc
        except KeyError as exc:
            raise AudioPipelineConfigError(str(exc.args[0])) from exc
        except (PermissionError, OSError, UnicodeDecodeError) as exc:
            raise AudioPipelineConfigError(UNREADABLE_CHARACTER_CARD_MESSAGE) from exc
        except ValueError as exc:
            raise AudioPipelineConfigError(str(exc)) from exc

        return AudioPipelineSession(
            tts_config=tts_config,
            transcriber=self._transcriber,
            speech_synthesizer=self._speech_synthesizer,
        )

    def close(self) -> None:
        close_transcriber = getattr(self._transcriber, "close", None)
        if callable(close_transcriber):
            close_transcriber()
        self._speech_synthesizer.close()


def resolve_audio_runtime_config(
    model_settings: ModelSettings, runtime_paths: RuntimePaths
) -> AudioRuntimeConfig:
    from app.stt.remote_whisper_client import (
        DEFAULT_WHISPER_BASE_URL,
        WHISPER_BASE_URL_ENV,
    )

    configured_url = os.environ.get(VOICEVOX_BASE_URL_ENV)
    if not configured_url:
        voicevox_base_url = DEFAULT_VOICEVOX_BASE_URL
    else:
        voicevox_base_url = configured_url.rstrip("/")
    return AudioRuntimeConfig(
        voicevox_base_url=voicevox_base_url,
        model_settings=model_settings,
        whisper_base_url=os.environ.get(
            WHISPER_BASE_URL_ENV, DEFAULT_WHISPER_BASE_URL
        ).rstrip("/"),
    )


def create_audio_pipeline_service(
    runtime_config: AudioRuntimeConfig,
) -> AudioPipelineService:
    from app.stt.remote_whisper_client import RemoteWhisperTranscriber

    return AudioPipelineService(
        RemoteWhisperTranscriber(runtime_config.whisper_base_url),
        create_voicevox_client(runtime_config.voicevox_base_url),
    )
