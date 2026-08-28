from enum import Enum
from pydantic import BaseModel
from uuid import UUID
from typing import Optional, List, Union


class Classification(Enum):
    RECOVERABLE = "recoverable"
    TERMINAL = "terminal"


class ClockDomain(Enum):
    CLIENT_MONOTONIC = "client_monotonic"
    SERVER_MONOTONIC = "server_monotonic"


class Measurement(Enum):
    FIRST_AUDIO_OUT = "first_audio_out"
    PLAYBACK_STARTED = "playback_started"
    RESPONSE_STARTED = "response_started"
    SPEECH_STOPPED = "speech_stopped"
    UTTERANCE_FINALIZED = "utterance_finalized"


class ProtocolVersion(Enum):
    THE_10 = "1.0"


class Reason(Enum):
    BARGE_IN = "barge_in"
    DECODE_FAILURE = "decode_failure"
    DISCONNECT = "disconnect"
    INPUT_CAPACITY_EXCEEDED = "input_capacity_exceeded"
    INVALID_AUDIO = "invalid_audio"
    PRIVACY = "privacy"
    RECONNECT_TIMEOUT = "reconnect_timeout"
    SESSION_ENDED = "session_ended"
    TERMINAL_ERROR = "terminal_error"
    USER_REQUEST = "user_request"


class Role(Enum):
    CHARACTER = "character"
    USER = "user"


class Speaker(BaseModel):
    participant_id: UUID
    role: Role
    character_id: Optional[str] = None


class TextRange(BaseModel):
    """生成本文を Unicode code point の半開区間 [start, end) で指す。0 <= start <= end を満たす。"""

    end: int
    start: int


class TypeEnum(Enum):
    ERROR = "error"
    OBSERVATION = "observation"
    PLAYBACK_COMPLETED = "playback_completed"
    PLAYBACK_DECODE_FAILED = "playback_decode_failed"
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_STOPPED = "playback_stopped"
    RESPONSE_AUDIO_SEGMENT = "response_audio_segment"
    RESPONSE_CANCELLED = "response_cancelled"
    RESPONSE_CANCEL_REQUESTED = "response_cancel_requested"
    RESPONSE_COMPLETED = "response_completed"
    RESPONSE_DELTA = "response_delta"
    RESPONSE_FAILED = "response_failed"
    RESPONSE_STARTED = "response_started"
    SESSION_DISCONNECTED = "session_disconnected"
    SESSION_ENDED = "session_ended"
    SESSION_MUTED = "session_muted"
    SESSION_RECONNECTED = "session_reconnected"
    SESSION_RECONNECT_REQUESTED = "session_reconnect_requested"
    SESSION_RESUMED = "session_resumed"
    SESSION_STARTED = "session_started"
    SESSION_START_REQUESTED = "session_start_requested"
    SPEECH_STARTED = "speech_started"
    SPEECH_STOPPED = "speech_stopped"
    UTTERANCE_DISCARDED = "utterance_discarded"
    UTTERANCE_FINALIZED = "utterance_finalized"
    UTTERANCE_PENDING = "utterance_pending"


class Unit(Enum):
    MILLISECOND = "millisecond"
    NANOSECOND = "nanosecond"


class UserState(Enum):
    ENDED = "ended"
    ERROR = "error"
    LISTENING = "listening"
    MUTED = "muted"
    RECONNECTING = "reconnecting"


class VoiceSessionEvent(BaseModel):
    """transport 非依存の双方向音声セッションイベント契約。音声バイト列はこのイベント契約の外側にある一時 media として扱う。"""

    event_id: UUID
    protocol_version: ProtocolVersion
    session_id: UUID
    type: TypeEnum
    monotonic_timestamp_ms: Optional[int] = None
    requested_reconnect_grace_ms: Optional[int] = None
    reconnect_grace_ms: Optional[int] = None
    reason: Optional[Reason] = None
    speaker: Optional[Speaker] = None
    utterance_id: Optional[UUID] = None
    should_response: Optional[bool] = None
    transcript: Optional[str] = None
    response_id: Optional[UUID] = None
    source_utterance_ids: Optional[List[UUID]] = None
    text: Optional[str] = None
    text_range: Optional[TextRange] = None
    text_sequence: Optional[int] = None
    audio_sequence: Optional[int] = None
    last_audio_sequence: Optional[int] = None
    last_text_sequence: Optional[int] = None
    error_code: Optional[str] = None
    recoverable: Optional[bool] = None
    last_played_audio_sequence: Optional[int] = None
    classification: Optional[Classification] = None
    user_state: Optional[UserState] = None
    clock_domain: Optional[ClockDomain] = None
    measurement: Optional[Measurement] = None
    timestamp: Optional[Union[int, str]] = None
    unit: Optional[Unit] = None
