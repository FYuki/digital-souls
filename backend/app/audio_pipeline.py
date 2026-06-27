import os
from contextlib import contextmanager
from dataclasses import dataclass
import logging
from time import perf_counter
from typing import Callable, Iterator, Protocol

from app.characters.loader import VoicevoxTtsConfig, load_tts_config
from app.tts.speech_synthesizer import SpeechSynthesizer
from app.tts.voicevox_client import (
    DEFAULT_VOICEVOX_BASE_URL,
    VOICEVOX_BASE_URL_ENV,
    create_voicevox_client,
)

logger = logging.getLogger(__name__)
ReplyGenerator = Callable[[str], str]
CLIENT_INPUT_ERROR_STATUS = 422
UPSTREAM_SERVICE_ERROR_STATUS = 502
UNREADABLE_CHARACTER_CARD_MESSAGE = "character card is not readable"
PCM_SAMPLE_WIDTH_BYTES = 2


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


class AudioPipelineSession:
    def __init__(
        self,
        tts_config: VoicevoxTtsConfig,
        transcriber: SpeechTranscriber,
        speech_synthesizer: SpeechSynthesizer,
    ) -> None:
        self._tts_config = tts_config
        self._transcriber = transcriber
        self._speech_synthesizer = speech_synthesizer

    def generate_response_audio(
        self,
        audio: bytes,
        reply_generator: ReplyGenerator,
    ) -> bytes:
        message = self._transcribe_audio(audio)
        reply = self._generate_reply(reply_generator, message)
        return self._synthesize_reply(reply)

    def _transcribe_audio(self, audio: bytes) -> str:
        _validate_pcm16_audio(audio)
        with _log_step_latency("STT"):
            try:
                return self._transcriber.transcribe(audio)
            except Exception as exc:
                logger.exception("STT failed")
                raise AudioPipelineStepError(
                    UPSTREAM_SERVICE_ERROR_STATUS,
                    "STT request failed",
                ) from exc

    def _generate_reply(self, reply_generator: ReplyGenerator, message: str) -> str:
        with _log_step_latency("LLM"):
            return reply_generator(message)

    def _synthesize_reply(self, reply: str) -> bytes:
        with _log_step_latency("VOICEVOX"):
            try:
                return self._speech_synthesizer.synthesize(
                    reply,
                    self._tts_config.speaker_id,
                )
            except Exception as exc:
                logger.exception("VOICEVOX synthesis failed")
                raise AudioPipelineStepError(
                    UPSTREAM_SERVICE_ERROR_STATUS,
                    "VOICEVOX request failed",
                ) from exc


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
        self._speech_synthesizer.close()


def resolve_audio_runtime_config() -> AudioRuntimeConfig:
    configured_url = os.environ.get(VOICEVOX_BASE_URL_ENV)
    if configured_url is None:
        voicevox_base_url = DEFAULT_VOICEVOX_BASE_URL
    else:
        voicevox_base_url = configured_url.rstrip("/")
    return AudioRuntimeConfig(voicevox_base_url=voicevox_base_url)


def create_audio_pipeline_service(
    runtime_config: AudioRuntimeConfig,
) -> AudioPipelineService:
    from app.stt.whisper_client import WhisperTranscriber

    return AudioPipelineService(
        WhisperTranscriber(),
        create_voicevox_client(runtime_config.voicevox_base_url),
    )
