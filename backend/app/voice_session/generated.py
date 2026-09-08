from enum import Enum
from pydantic import BaseModel
from typing import Optional, List, Union
from uuid import UUID


class Classification(Enum):
    RECOVERABLE = "recoverable"
    TERMINAL = "terminal"


class ClockDomain(Enum):
    CLIENT_MONOTONIC = "client_monotonic"
    SERVER_MONOTONIC = "server_monotonic"


class Decision(Enum):
    BACKCHANNEL = "backchannel"
    INDETERMINATE = "indeterminate"
    TAKE_TURN = "take_turn"


class Measurement(Enum):
    CANCEL_CONFIRMED = "cancel_confirmed"
    CLIENT_AUDIO_DECODED = "client_audio_decoded"
    CLIENT_ENCODED_RECEIVED = "client_encoded_received"
    CLIENT_TRACK_RECEIVED = "client_track_received"
    FIRST_AUDIO_OUT = "first_audio_out"
    LOCAL_PLAYBACK_STOPPED = "local_playback_stopped"
    NETWORK_SUMMARY = "network_summary"
    PLAYBACK_STARTED = "playback_started"
    RESPONSE_STARTED = "response_started"
    SESSION_SUMMARY = "session_summary"
    SPEECH_STOPPED = "speech_stopped"
    TURN_DECISION_RECEIVED = "turn_decision_received"
    UTTERANCE_FINALIZED = "utterance_finalized"


class DownlinkReason(Enum):
    AMBIGUOUS_AUDIO_STREAM = "ambiguous_audio_stream"
    COUNTER_REGRESSED = "counter_regressed"
    INVALID_RTP_COUNTERS = "invalid_rtp_counters"
    PLAYBACK_PACKETS_NOT_YET_REPORTED = "playback_packets_not_yet_reported"
    STATS_API_UNAVAILABLE = "stats_api_unavailable"
    STATS_FAILED = "stats_failed"
    STATS_TIMEOUT = "stats_timeout"
    STATS_UNAVAILABLE = "stats_unavailable"


class Status(Enum):
    MEASURED = "measured"
    MISSING = "missing"


class Downlink(BaseModel):
    status: Status
    bytes: Optional[int] = None
    lost_packets: Optional[int] = None
    packets: Optional[int] = None
    reason: Optional[DownlinkReason] = None


class Method(Enum):
    BROWSER_AUDIO_RTP_COUNTERS_V1 = "browser_audio_rtp_counters_v1"


class Uplink(BaseModel):
    status: Status
    bytes: Optional[int] = None
    packets: Optional[int] = None
    reason: Optional[DownlinkReason] = None


class NetworkSummary(BaseModel):
    downlink: Downlink
    method: Method
    uplink: Uplink


class PlaybackSummary(BaseModel):
    confirmation_observed_at_ms: float
    expected_samples: int
    first_output_frame: int
    first_rtp_timestamp: int
    gap_count: int
    gap_samples: int
    input_samples: int
    last_output_end_frame: int
    last_rtp_timestamp: int
    maximum_gap_samples: int
    output_clock_context_time: float
    output_clock_performance_time: float
    packet_count: int
    padding_samples: int
    rendered_samples: int
    sample_rate: int


class ProtocolVersion(Enum):
    THE_10 = "1.0"


class VoiceSessionEventReason(Enum):
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


class SessionSummary(BaseModel):
    end_requested: bool
    microphone_activation_attempts: int
    mute_attempts: int
    operation_tracking_started: bool
    retry_attempts: int
    sequence: int


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
    TURN_DECISION = "turn_decision"
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
    reason: Optional[VoiceSessionEventReason] = None
    response_id: Optional[UUID] = None
    speaker: Optional[Speaker] = None
    utterance_id: Optional[UUID] = None
    decision: Optional[Decision] = None
    final: Optional[bool] = None
    should_response: Optional[bool] = None
    transcript: Optional[str] = None
    history_turn_id: Optional[UUID] = None
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
    playback_summary: Optional[PlaybackSummary] = None
    response_finished: Optional[bool] = None
    classification: Optional[Classification] = None
    user_state: Optional[UserState] = None
    clock_domain: Optional[ClockDomain] = None
    measurement: Optional[Measurement] = None
    network_summary: Optional[NetworkSummary] = None
    session_summary: Optional[SessionSummary] = None
    timestamp: Optional[Union[int, str]] = None
    unit: Optional[Unit] = None
