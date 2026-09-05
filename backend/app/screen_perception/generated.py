from enum import Enum
from pydantic import BaseModel
from typing import List, Optional
from uuid import UUID


class ActualSurface(Enum):
    MONITOR = "monitor"
    WINDOW = "window"


class CaptureState(Enum):
    ACTIVE = "active"
    OFF = "off"
    SELECTING = "selecting"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


class ChatDestination(Enum):
    CLOUD = "cloud"
    LOCAL = "local"


class CloudConsent(BaseModel):
    cloud_derived_chat: bool
    cloud_vision: bool


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


class Reason(Enum):
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


class ScreenPerceptionSchema(Enum):
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


class RecognitionState(Enum):
    CAPTURING = "capturing"
    COMPOSING = "composing"
    FAILED = "failed"
    IDLE = "idle"
    RECOGNIZING = "recognizing"
    SNAPSHOT_REQUESTED = "snapshot_requested"
    SUCCEEDED = "succeeded"
    UPLOADING = "uploading"


class Source(Enum):
    EXPLICIT_UI = "explicit_ui"
    NATURAL_LANGUAGE_TEXT = "natural_language_text"
    NATURAL_LANGUAGE_VOICE = "natural_language_voice"


class Stage(Enum):
    CAPTURE = "capture"
    CHAT = "chat"
    PICKER = "picker"
    SESSION = "session"
    SUPPORT = "support"
    UPLOAD = "upload"
    VISION = "vision"


class TypeEnum(Enum):
    SCREEN_ERROR = "screen_error"
    SCREEN_ROUTING_DISCLOSED = "screen_routing_disclosed"
    SCREEN_SESSION_HEARTBEAT = "screen_session_heartbeat"
    SCREEN_SESSION_HEARTBEAT_ACCEPTED = "screen_session_heartbeat_accepted"
    SCREEN_SESSION_REVOKED = "screen_session_revoked"
    SCREEN_SESSION_REVOKE_REQUESTED = "screen_session_revoke_requested"
    SCREEN_SESSION_STARTED = "screen_session_started"
    SCREEN_SESSION_START_REQUESTED = "screen_session_start_requested"
    SCREEN_SNAPSHOT_REQUESTED = "screen_snapshot_requested"
    SCREEN_SNAPSHOT_UPLOAD_ACCEPTED = "screen_snapshot_upload_accepted"
    SCREEN_SNAPSHOT_UPLOAD_METADATA = "screen_snapshot_upload_metadata"
    SCREEN_STATUS = "screen_status"


class VisionDestination(Enum):
    CLOUD = "cloud"
    LOCAL = "local"
    UNCONFIGURED = "unconfigured"


class ScreenPerceptionEvent(BaseModel):
    """画面共有の制御metadata契約。画像本文、質問本文、Vision観測本文、対象名は含めない。"""

    event_id: UUID
    protocol_version: ProtocolVersion
    type: TypeEnum
    chat_destination: Optional[ChatDestination] = None
    client_session_id: Optional[UUID] = None
    limits: Optional[Limits] = None
    routing_revision: Optional[str] = None
    vision_destination: Optional[VisionDestination] = None
    actual_surface: Optional[ActualSurface] = None
    character_id: Optional[str] = None
    cloud_consent: Optional[CloudConsent] = None
    conversation_id: Optional[UUID] = None
    generation: Optional[int] = None
    requested_surface: Optional[ActualSurface] = None
    heartbeat_interval_ms: Optional[int] = None
    lease_duration_ms: Optional[int] = None
    lease_expires_at: Optional[str] = None
    screen_session_id: Optional[UUID] = None
    reason: Optional[Reason] = None
    capture_deadline: Optional[str] = None
    request_id: Optional[UUID] = None
    requested_at: Optional[str] = None
    source: Optional[Source] = None
    turn_id: Optional[UUID] = None
    byte_length: Optional[int] = None
    captured_at: Optional[str] = None
    height: Optional[int] = None
    image_id: Optional[UUID] = None
    mime_type: Optional[MIMEType] = None
    width: Optional[int] = None
    received_at: Optional[str] = None
    capture_state: Optional[CaptureState] = None
    last_recognized_capture_at: Optional[str] = None
    reason_code: Optional[ScreenPerceptionSchema] = None
    recognition_state: Optional[RecognitionState] = None
    recoverable: Optional[bool] = None
    stage: Optional[Stage] = None
