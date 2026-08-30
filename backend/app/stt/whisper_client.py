import io
import logging
import threading
import wave
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol, cast

from app.audio.constants import (
    PCM_CHANNELS,
    PCM_SAMPLE_RATE_HZ,
    PCM_SAMPLE_WIDTH_BYTES,
)

WHISPER_LANGUAGE = "ja"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"

logger = logging.getLogger(__name__)


class WhisperSegment(Protocol):
    text: str


class WhisperModel(Protocol):
    def transcribe(
        self,
        audio_source: io.BytesIO,
        *,
        language: str,
    ) -> tuple[Iterable[WhisperSegment], object]:
        pass


class WhisperTranscriber:
    def __init__(self, *, model_name: str, download_root: Path) -> None:
        self._model_name = model_name
        self._download_root = download_root
        self._model: WhisperModel | None = None
        self._model_lock = threading.Lock()
        logger.info(
            "Whisper transcriber initialized: device=%s compute_type=%s",
            self.device,
            self.compute_type,
        )

    @property
    def device(self) -> str:
        return WHISPER_DEVICE

    @property
    def compute_type(self) -> str:
        return WHISPER_COMPUTE_TYPE

    def transcribe(self, audio: bytes) -> str:
        audio_source = _pcm16_16khz_to_wav(audio)
        with self._model_lock:
            model = self._get_or_create_model()
            segments, _info = model.transcribe(
                audio_source,
                language=WHISPER_LANGUAGE,
            )
            return "".join(_segment_text(segment) for segment in segments)

    def _get_or_create_model(self) -> WhisperModel:
        if self._model is None:
            from faster_whisper import (
                WhisperModel as FasterWhisperModel,
            )

            self._model = cast(
                WhisperModel,
                FasterWhisperModel(
                    self._model_name,
                    download_root=str(self._download_root),
                    device=self.device,
                    compute_type=self.compute_type,
                ),
            )
        return self._model


def _pcm16_16khz_to_wav(audio: bytes) -> io.BytesIO:
    audio_source = io.BytesIO()
    with wave.open(audio_source, "wb") as wav_file:
        wav_file.setnchannels(PCM_CHANNELS)
        wav_file.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(PCM_SAMPLE_RATE_HZ)
        wav_file.writeframes(audio)
    audio_source.seek(0)
    return audio_source


def _segment_text(segment: WhisperSegment) -> str:
    return segment.text
