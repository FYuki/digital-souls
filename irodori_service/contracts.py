from __future__ import annotations

import io
import math
import re
import wave
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_AUDIO_BYTES = 48_000 * 2 * 60 + 4096


class ServiceError(RuntimeError):
    def __init__(self, code: str, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class SynthesisOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    caption: str = Field(max_length=4096)
    seed: int = Field(ge=0, le=2**32 - 1)
    num_steps: int = Field(default=40, ge=1, le=100)
    chunking_enabled: Literal[False] = False


class SpeechRequest(BaseModel):
    """区間合成だけを公開し、任意path・参照なし合成・SSEを受け付けない。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    model: Literal["irodori-tts"] = "irodori-tts"
    input: str = Field(min_length=1, max_length=4096)
    voice: str
    response_format: Literal["wav"] = "wav"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    irodori: SynthesisOptions

    @field_validator("voice")
    @classmethod
    def validate_voice(cls, value: str) -> str:
        if not VOICE_ID_PATTERN.fullmatch(value) or value.lower() in {
            "none", "no_ref", "no-ref", "null", "text-only"
        }:
            raise ValueError("registered voice ID is required")
        return value

    @field_validator("input")
    @classmethod
    def validate_input(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must contain text")
        return value

    @field_validator("speed")
    @classmethod
    def validate_speed(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("speed must be finite")
        return value


def validate_wav(audio: bytes) -> None:
    """切れたWAVや不正形式を準備完了・合成成功として渡さない。"""
    if len(audio) > MAX_AUDIO_BYTES:
        raise ServiceError("tts_invalid_audio")
    try:
        with wave.open(io.BytesIO(audio), "rb") as stream:
            frames = stream.getnframes()
            if (
                stream.getcomptype() != "NONE"
                or stream.getnchannels() != 1
                or stream.getsampwidth() != 2
                or stream.getframerate() != 48_000
                or frames < 1
            ):
                raise ServiceError("tts_invalid_audio")
            pcm = stream.readframes(frames)
            if len(pcm) != frames * 2 or not any(pcm):
                raise ServiceError("tts_invalid_audio")
    except (wave.Error, EOFError, OSError) as error:
        raise ServiceError("tts_invalid_audio") from error
