import io
import threading
import wave
from collections.abc import Iterable
from typing import Protocol, cast

WHISPER_MODEL_SIZE = "medium"
WHISPER_LANGUAGE = "ja"
PCM_CHANNELS = 1
PCM_SAMPLE_WIDTH_BYTES = 2
PCM_FRAME_RATE = 16_000


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
    def __init__(self) -> None:
        self._model: WhisperModel | None = None
        self._model_lock = threading.Lock()

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
            from faster_whisper import (  # type: ignore[import-untyped]
                WhisperModel as FasterWhisperModel,
            )

            self._model = cast(
                WhisperModel,
                FasterWhisperModel(WHISPER_MODEL_SIZE),
            )
        return self._model


def _pcm16_16khz_to_wav(audio: bytes) -> io.BytesIO:
    audio_source = io.BytesIO()
    with wave.open(audio_source, "wb") as wav_file:
        wav_file.setnchannels(PCM_CHANNELS)
        wav_file.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(PCM_FRAME_RATE)
        wav_file.writeframes(audio)
    audio_source.seek(0)
    return audio_source


def _segment_text(segment: WhisperSegment) -> str:
    return segment.text
