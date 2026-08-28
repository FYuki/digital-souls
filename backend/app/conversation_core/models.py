from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResponseState(Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    PRIVACY_SKIPPED = "privacy_skipped"

    @property
    def is_terminal(self) -> bool:
        return self is not ResponseState.IN_PROGRESS


class UtteranceState(Enum):
    PENDING = "pending"
    CONSUMED = "consumed"
    DISCARDED = "discarded"


@dataclass(frozen=True)
class Utterance:
    utterance_id: str
    transcript: str
    should_response: bool
    state: UtteranceState
    discard_reason: str | None = None


@dataclass(frozen=True)
class TextDelta:
    text_sequence: int
    text: str
    text_range: tuple[int, int]


@dataclass(frozen=True)
class AudioSegment:
    audio_sequence: int
    audio: bytes
    text_range: tuple[int, int]


@dataclass(frozen=True)
class Response:
    response_id: str
    generation: int
    source_utterance_ids: tuple[str, ...]
    state: ResponseState
    generated_text: str = ""
    audio_segments: tuple[AudioSegment, ...] = ()
    last_played_audio_sequence: int = 0
    terminal_reason: str | None = None


@dataclass(frozen=True)
class TerminalOutcome:
    response_id: str
    generation: int
    state: ResponseState
    reason: str | None
    generated_text: str
    audio_segments: tuple[AudioSegment, ...]
    last_played_audio_sequence: int
    source_utterance_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResponseStartResult:
    content_skipped: bool


@dataclass(frozen=True)
class CoreEvent:
    type: str
    session_id: str
    response_id: str | None = None
    generation: int | None = None
    source_utterance_ids: tuple[str, ...] | None = None
    text_sequence: int | None = None
    text: str | None = None
    text_range: tuple[int, int] | None = None
    audio_sequence: int | None = None
    audio: bytes | None = None
    reason: str | None = None
    last_text_sequence: int | None = None
    last_audio_sequence: int | None = None


@dataclass(frozen=True)
class StageObservation:
    session_id: str
    response_id: str | None
    generation: int | None
    stage: str
    outcome: str
    utterance_id: str | None = None
