from enum import Enum
from pydantic import BaseModel
from typing import List, Optional, Union
from uuid import UUID


class ChatDestination(Enum):
    CLOUD = "cloud"
    LOCAL = "local"


class MIMEType(Enum):
    IMAGE_JPEG = "image/jpeg"
    IMAGE_PNG = "image/png"


class Limits(BaseModel):
    allowed_mime_types: List[MIMEType]
    capture_timeout_ms: int
    max_bytes: int
    max_concurrency: int
    max_height: int
    max_pixels: int
    max_width: int
    request_timeout_ms: int
    snapshot_max_age_ms: int
    vision_timeout_ms: int


class ProtocolVersion(Enum):
    THE_10 = "1.0"


class RoutingDisclosureType(Enum):
    SCREEN_ROUTING_DISCLOSED = "screen_routing_disclosed"


class VisionDestination(Enum):
    CLOUD = "cloud"
    LOCAL = "local"
    UNCONFIGURED = "unconfigured"


class RoutingDisclosure(BaseModel):
    chat_destination: ChatDestination
    client_session_id: UUID
    event_id: UUID
    limits: Limits
    protocol_version: ProtocolVersion
    routing_revision: str
    type: RoutingDisclosureType
    vision_destination: VisionDestination


class ReasonCode(Enum):
    API_UNAVAILABLE = "api_unavailable"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    BROWSER_SURFACE_REJECTED = "browser_surface_rejected"
    CAPTURE_NOT_ALLOWED = "capture_not_allowed"
    CAPTURE_OS_ERROR = "capture_os_error"
    CLOUD_CONSENT_REQUIRED = "cloud_consent_required"
    CONTEXT_MISMATCH = "context_mismatch"
    DUPLICATE_REQUEST = "duplicate_request"
    FRAME_UNAVAILABLE = "frame_unavailable"
    GENERATION_MISMATCH = "generation_mismatch"
    IMAGE_DECODE_FAILED = "image_decode_failed"
    IMAGE_TOO_LARGE = "image_too_large"
    IMAGE_TYPE_INVALID = "image_type_invalid"
    INSECURE_CONTEXT = "insecure_context"
    NO_CAPTURE_SOURCE = "no_capture_source"
    PICKER_CANCELLED = "picker_cancelled"
    REQUEST_CANCELLED = "request_cancelled"
    REQUEST_EXPIRED = "request_expired"
    REQUEST_NOT_FOUND = "request_not_found"
    ROUTING_CHANGED = "routing_changed"
    SESSION_EXPIRED = "session_expired"
    SESSION_NOT_FOUND = "session_not_found"
    SESSION_REVOKED = "session_revoked"
    SURFACE_MISMATCH = "surface_mismatch"
    SURFACE_UNKNOWN = "surface_unknown"
    TRANSIENT_ACTIVATION_REQUIRED = "transient_activation_required"
    VIDEO_TRACK_INVALID = "video_track_invalid"
    VISION_INVALID_RESPONSE = "vision_invalid_response"
    VISION_TIMEOUT = "vision_timeout"
    VISION_UNAVAILABLE = "vision_unavailable"
    VISION_UNCONFIGURED = "vision_unconfigured"
    VISION_UNSUPPORTED = "vision_unsupported"


class Stage(Enum):
    CAPTURE = "capture"
    CHAT = "chat"
    PICKER = "picker"
    SESSION = "session"
    SUPPORT = "support"
    UPLOAD = "upload"
    VISION = "vision"


class ScreenErrorType(Enum):
    SCREEN_ERROR = "screen_error"


class ScreenError(BaseModel):
    event_id: UUID
    generation: int
    protocol_version: ProtocolVersion
    reason_code: ReasonCode
    recoverable: bool
    stage: Stage
    type: ScreenErrorType
    request_id: Optional[UUID] = None
    screen_session_id: Optional[UUID] = None
    turn_id: Optional[UUID] = None


class CaptureState(Enum):
    ACTIVE = "active"
    OFF = "off"
    SELECTING = "selecting"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class RecognitionState(Enum):
    CAPTURING = "capturing"
    COMPOSING = "composing"
    FAILED = "failed"
    IDLE = "idle"
    RECOGNIZING = "recognizing"
    SNAPSHOT_REQUESTED = "snapshot_requested"
    SUCCEEDED = "succeeded"
    UPLOADING = "uploading"


class ScreenStatusType(Enum):
    SCREEN_STATUS = "screen_status"


class ScreenStatus(BaseModel):
    capture_state: CaptureState
    event_id: UUID
    generation: int
    protocol_version: ProtocolVersion
    recognition_state: RecognitionState
    type: ScreenStatusType
    last_recognized_capture_at: Optional[str] = None
    reason_code: Optional[ReasonCode] = None
    screen_session_id: Optional[UUID] = None


class SessionHeartbeatType(Enum):
    SCREEN_SESSION_HEARTBEAT = "screen_session_heartbeat"


class SessionHeartbeat(BaseModel):
    client_session_id: UUID
    event_id: UUID
    generation: int
    protocol_version: ProtocolVersion
    screen_session_id: UUID
    type: SessionHeartbeatType


class SessionHeartbeatAcceptedType(Enum):
    SCREEN_SESSION_HEARTBEAT_ACCEPTED = "screen_session_heartbeat_accepted"


class SessionHeartbeatAccepted(BaseModel):
    event_id: UUID
    generation: int
    lease_expires_at: str
    protocol_version: ProtocolVersion
    screen_session_id: UUID
    type: SessionHeartbeatAcceptedType


class SessionRevokeRequestedReason(Enum):
    BACKEND_DISCONNECT = "backend_disconnect"
    CAPTURE_ENDED = "capture_ended"
    CHARACTER_CHANGE = "character_change"
    CONSENT_REVOKED = "consent_revoked"
    CONVERSATION_CHANGE = "conversation_change"
    PAGEHIDE = "pagehide"
    TARGET_CHANGE = "target_change"
    USER_OFF = "user_off"


class SessionRevokeRequestedType(Enum):
    SCREEN_SESSION_REVOKE_REQUESTED = "screen_session_revoke_requested"


class SessionRevokeRequested(BaseModel):
    client_session_id: UUID
    event_id: UUID
    generation: int
    protocol_version: ProtocolVersion
    reason: SessionRevokeRequestedReason
    screen_session_id: UUID
    type: SessionRevokeRequestedType


class SessionRevokedReason(Enum):
    BACKEND_DISCONNECT = "backend_disconnect"
    BACKEND_RESTART = "backend_restart"
    CAPTURE_ENDED = "capture_ended"
    CHARACTER_CHANGE = "character_change"
    CONSENT_REVOKED = "consent_revoked"
    CONVERSATION_CHANGE = "conversation_change"
    LEASE_EXPIRED = "lease_expired"
    PAGEHIDE = "pagehide"
    ROUTING_CHANGED = "routing_changed"
    TARGET_CHANGE = "target_change"
    USER_OFF = "user_off"


class SessionRevokedType(Enum):
    SCREEN_SESSION_REVOKED = "screen_session_revoked"


class SessionRevoked(BaseModel):
    event_id: UUID
    generation: int
    protocol_version: ProtocolVersion
    reason: SessionRevokedReason
    screen_session_id: UUID
    type: SessionRevokedType


class ActualSurface(Enum):
    MONITOR = "monitor"
    WINDOW = "window"


class CloudConsent(BaseModel):
    cloud_derived_chat: bool
    cloud_vision: bool


class SessionStartRequestedType(Enum):
    SCREEN_SESSION_START_REQUESTED = "screen_session_start_requested"


class SessionStartRequested(BaseModel):
    actual_surface: ActualSurface
    character_id: str
    client_session_id: UUID
    cloud_consent: CloudConsent
    conversation_id: UUID
    event_id: UUID
    generation: int
    protocol_version: ProtocolVersion
    requested_surface: ActualSurface
    routing_revision: str
    type: SessionStartRequestedType


class SessionStartedType(Enum):
    SCREEN_SESSION_STARTED = "screen_session_started"


class SessionStarted(BaseModel):
    actual_surface: ActualSurface
    character_id: str
    client_session_id: UUID
    conversation_id: UUID
    event_id: UUID
    generation: int
    heartbeat_interval_ms: int
    lease_duration_ms: int
    lease_expires_at: str
    protocol_version: ProtocolVersion
    routing_revision: str
    screen_session_id: UUID
    type: SessionStartedType


class Source(Enum):
    EXPLICIT_UI = "explicit_ui"
    NATURAL_LANGUAGE_TEXT = "natural_language_text"
    NATURAL_LANGUAGE_VOICE = "natural_language_voice"


class SnapshotRequestedType(Enum):
    SCREEN_SNAPSHOT_REQUESTED = "screen_snapshot_requested"


class SnapshotRequested(BaseModel):
    capture_deadline: str
    event_id: UUID
    generation: int
    protocol_version: ProtocolVersion
    request_id: UUID
    requested_at: str
    screen_session_id: UUID
    source: Source
    turn_id: UUID
    type: SnapshotRequestedType


class SnapshotUploadAcceptedType(Enum):
    SCREEN_SNAPSHOT_UPLOAD_ACCEPTED = "screen_snapshot_upload_accepted"


class SnapshotUploadAccepted(BaseModel):
    event_id: UUID
    generation: int
    image_id: UUID
    protocol_version: ProtocolVersion
    received_at: str
    request_id: UUID
    screen_session_id: UUID
    type: SnapshotUploadAcceptedType


class SnapshotUploadMetadataType(Enum):
    SCREEN_SNAPSHOT_UPLOAD_METADATA = "screen_snapshot_upload_metadata"


class SnapshotUploadMetadata(BaseModel):
    actual_surface: ActualSurface
    byte_length: int
    captured_at: str
    client_session_id: UUID
    event_id: UUID
    generation: int
    height: int
    image_id: UUID
    mime_type: MIMEType
    protocol_version: ProtocolVersion
    request_id: UUID
    screen_session_id: UUID
    turn_id: UUID
    type: SnapshotUploadMetadataType
    width: int


ScreenPerceptionEvent = Union[
    RoutingDisclosure,
    SessionStartRequested,
    SessionStarted,
    SessionHeartbeat,
    SessionHeartbeatAccepted,
    SessionRevokeRequested,
    SessionRevoked,
    SnapshotRequested,
    SnapshotUploadMetadata,
    SnapshotUploadAccepted,
    ScreenStatus,
    ScreenError,
]
