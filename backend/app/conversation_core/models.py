from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class ResponseState(Enum):
    IN_PROGRESS = "in_progress"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    PRIVACY_SKIPPED = "privacy_skipped"

    @property
    def is_terminal(self) -> bool:
        return self not in {ResponseState.IN_PROGRESS, ResponseState.CANCELLING}


class UtteranceState(Enum):
    PENDING = "pending"
    CONSUMED = "consumed"
    DISCARDED = "discarded"


@dataclass(frozen=True)
class InputSource:
    input_id: str
    source: Literal["speech", "text"]


@dataclass(frozen=True)
class UserInput:
    """STTで確定した音声と直接入力されたテキストの共通境界。"""

    input_id: str
    source: Literal["speech", "text"]
    text: str
    should_response: bool
    state: UtteranceState
    discard_reason: str | None = None
    control_request_id: str | None = None

    @property
    def reference(self) -> InputSource:
        return InputSource(self.input_id, self.source)


@dataclass(frozen=True)
class Utterance:
    utterance_id: str
    transcript: str
    should_response: bool
    state: UtteranceState
    discard_reason: str | None = None
    control_request_id: str | None = None


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
    source_inputs: tuple[InputSource, ...] = ()
    generated_text: str = ""
    last_text_sequence: int = 0
    audio_segments: tuple[AudioSegment, ...] = ()
    last_played_audio_sequence: int = 0
    terminal_reason: str | None = None


@dataclass(frozen=True)
class ResponseStopResult:
    """送出と最終出力の停止確認後に得た再生済みprefix。"""

    last_played_audio_sequence: int


@dataclass(frozen=True)
class TerminalOutcome:
    response_id: str
    generation: int
    state: ResponseState
    reason: str | None
    generated_text: str
    audio_segments: tuple[AudioSegment, ...]
    last_played_audio_sequence: int
    last_text_sequence: int = 0
    source_utterance_ids: tuple[str, ...] = ()
    source_inputs: tuple[InputSource, ...] = ()
    terminal_state_bounds_ns: tuple[int, int] | None = None


@dataclass(frozen=True)
class ResponseStartResult:
    content_skipped: bool
    history_turn_id: str | None = None


@dataclass(frozen=True)
class CoreEvent:
    type: str
    session_id: str
    utterance_id: str | None = None
    transcript: str | None = None
    should_response: bool | None = None
    classification: str | None = None
    decision: str | None = None
    final: bool | None = None
    error_code: str | None = None
    recoverable: bool | None = None
    user_state: str | None = None
    response_id: str | None = None
    generation: int | None = None
    source_utterance_ids: tuple[str, ...] | None = None
    source_inputs: tuple[InputSource, ...] | None = None
    text_sequence: int | None = None
    text: str | None = None
    text_range: tuple[int, int] | None = None
    audio_sequence: int | None = None
    audio: bytes | None = None
    reason: str | None = None
    last_text_sequence: int | None = None
    last_audio_sequence: int | None = None
    terminal_state_bounds_ns: tuple[int, int] | None = None
    history_turn_id: str | None = None


@dataclass(frozen=True)
class StageObservation:
    session_id: str
    response_id: str | None
    generation: int | None
    stage: str
    outcome: str
    utterance_id: str | None = None
    timestamp_ns: int | None = None
    value: float | None = None
