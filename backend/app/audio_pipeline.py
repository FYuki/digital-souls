import os
from dataclasses import dataclass
import logging
from time import perf_counter
from typing import Callable

from app.characters.loader import VoicevoxTtsConfig, load_tts_config
from app.stt.whisper_client import WhisperTranscriber
from app.tts.voicevox_client import (
    DEFAULT_VOICEVOX_BASE_URL,
    VOICEVOX_BASE_URL_ENV,
    synthesize,
)

logger = logging.getLogger(__name__)
ReplyGenerator = Callable[[str], str]


class AudioPipelineConfigError(ValueError):
    """Server-side audio pipeline configuration is invalid."""


class AudioPipelineStepError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class AudioRuntimeConfig:
    voicevox_base_url: str


@dataclass(frozen=True)
class AudioPipelineSession:
    tts_config: VoicevoxTtsConfig
    voicevox_base_url: str
    transcriber: WhisperTranscriber

    def generate_response_audio(
        self,
        audio: bytes,
        reply_generator: ReplyGenerator,
    ) -> bytes:
        message = self._transcribe_audio(audio)
        reply = self._generate_reply(reply_generator, message)
        return self._synthesize_reply(reply)

    def _transcribe_audio(self, audio: bytes) -> str:
        started_at = perf_counter()
        try:
            return self.transcriber.transcribe(audio)
        except Exception as exc:
            logger.exception("STT failed")
            raise AudioPipelineStepError(502, "STT request failed") from exc
        finally:
            logger.info("STT completed in %.3fs", perf_counter() - started_at)

    def _generate_reply(self, reply_generator: ReplyGenerator, message: str) -> str:
        started_at = perf_counter()
        try:
            return reply_generator(message)
        finally:
            logger.info("LLM completed in %.3fs", perf_counter() - started_at)

    def _synthesize_reply(self, reply: str) -> bytes:
        started_at = perf_counter()
        try:
            return synthesize(reply, self.tts_config.speaker_id, self.voicevox_base_url)
        except Exception as exc:
            logger.exception("VOICEVOX synthesis failed")
            raise AudioPipelineStepError(502, "VOICEVOX request failed") from exc
        finally:
            logger.info("VOICEVOX completed in %.3fs", perf_counter() - started_at)


class AudioPipelineService:
    def __init__(
        self,
        runtime_config: AudioRuntimeConfig,
        transcriber: WhisperTranscriber,
    ) -> None:
        self._runtime_config = runtime_config
        self._transcriber = transcriber

    def create_session(self, character: str) -> AudioPipelineSession:
        try:
            tts_config = load_tts_config(character)
        except FileNotFoundError as exc:
            raise AudioPipelineConfigError("character card is required") from exc
        except KeyError as exc:
            raise AudioPipelineConfigError("tts_config is required") from exc
        except ValueError as exc:
            raise AudioPipelineConfigError(str(exc)) from exc

        return AudioPipelineSession(
            tts_config=tts_config,
            voicevox_base_url=self._runtime_config.voicevox_base_url,
            transcriber=self._transcriber,
        )


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
    return AudioPipelineService(runtime_config, WhisperTranscriber())
