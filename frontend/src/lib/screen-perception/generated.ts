/**
 * 画面共有の制御metadata契約。画像本文、質問本文、Vision観測本文、対象名は含めない。
 */
export type ScreenPerceptionEvent =
  | RoutingDisclosure
  | SessionStartRequested
  | SessionStarted
  | SessionHeartbeat
  | SessionHeartbeatAccepted
  | SessionRevokeRequested
  | SessionRevoked
  | SnapshotRequested
  | SnapshotUploadMetadata
  | SnapshotUploadAccepted
  | ScreenStatus
  | ScreenError;

export interface RoutingDisclosure {
    chat_destination:   ChatDestination;
    client_session_id:  string;
    event_id:           string;
    limits:             Limits;
    protocol_version:   "1.0";
    routing_revision:   string;
    type:               "screen_routing_disclosed";
    vision_destination: VisionDestination;
}

export type ChatDestination = "local" | "cloud";

export interface Limits {
    allowed_mime_types:  MIMEType[];
    capture_timeout_ms:  number;
    max_bytes:           number;
    max_concurrency:     number;
    max_height:          number;
    max_pixels:          number;
    max_width:           number;
    request_timeout_ms:  number;
    snapshot_max_age_ms: number;
    vision_timeout_ms:   number;
}

export type MIMEType = "image/png" | "image/jpeg";

export type VisionDestination = "local" | "cloud" | "unconfigured";

export interface ScreenError {
    event_id:           string;
    generation:         number;
    protocol_version:   "1.0";
    reason_code:        ReasonCode;
    recoverable:        boolean;
    request_id?:        string;
    screen_session_id?: string;
    stage:              Stage;
    turn_id?:           string;
    type:               "screen_error";
}

export type ReasonCode = "api_unavailable" | "insecure_context" | "transient_activation_required" | "capture_not_allowed" | "no_capture_source" | "capture_os_error" | "picker_cancelled" | "surface_mismatch" | "browser_surface_rejected" | "surface_unknown" | "video_track_invalid" | "frame_unavailable" | "image_type_invalid" | "image_too_large" | "image_decode_failed" | "session_not_found" | "session_revoked" | "session_expired" | "generation_mismatch" | "context_mismatch" | "request_not_found" | "request_expired" | "duplicate_request" | "cloud_consent_required" | "routing_changed" | "vision_unconfigured" | "vision_unsupported" | "vision_timeout" | "vision_unavailable" | "vision_invalid_response" | "request_cancelled" | "backend_unavailable";

export type Stage = "support" | "picker" | "capture" | "session" | "upload" | "vision" | "chat";

export interface ScreenStatus {
    capture_state:              CaptureState;
    event_id:                   string;
    generation:                 number;
    last_recognized_capture_at: null | string;
    protocol_version:           "1.0";
    reason_code:                ReasonCode | null;
    recognition_state:          RecognitionState;
    screen_session_id?:         null | string;
    type:                       "screen_status";
}

export type CaptureState = "off" | "selecting" | "active" | "unavailable" | "unsupported";

export type RecognitionState = "idle" | "snapshot_requested" | "capturing" | "uploading" | "recognizing" | "composing" | "succeeded" | "failed";

export interface SessionHeartbeat {
    client_session_id: string;
    event_id:          string;
    generation:        number;
    protocol_version:  "1.0";
    screen_session_id: string;
    type:              "screen_session_heartbeat";
}

export interface SessionHeartbeatAccepted {
    event_id:          string;
    generation:        number;
    lease_expires_at:  string;
    protocol_version:  "1.0";
    screen_session_id: string;
    type:              "screen_session_heartbeat_accepted";
}

export interface SessionRevokeRequested {
    client_session_id: string;
    event_id:          string;
    generation:        number;
    protocol_version:  "1.0";
    reason:            SessionRevokeRequestedReason;
    screen_session_id: string;
    type:              "screen_session_revoke_requested";
}

export type SessionRevokeRequestedReason = "user_off" | "target_change" | "conversation_change" | "character_change" | "capture_ended" | "pagehide" | "backend_disconnect" | "consent_revoked";

export interface SessionRevoked {
    event_id:          string;
    generation:        number;
    protocol_version:  "1.0";
    reason:            SessionRevokedReason;
    screen_session_id: string;
    type:              "screen_session_revoked";
}

export type SessionRevokedReason = "user_off" | "target_change" | "conversation_change" | "character_change" | "capture_ended" | "pagehide" | "backend_disconnect" | "consent_revoked" | "lease_expired" | "routing_changed" | "backend_restart";

export interface SessionStartRequested {
    actual_surface:    ActualSurface;
    character_id:      string;
    client_session_id: string;
    cloud_consent:     CloudConsent;
    conversation_id:   string;
    event_id:          string;
    generation:        number;
    protocol_version:  "1.0";
    requested_surface: ActualSurface;
    routing_revision:  string;
    type:              "screen_session_start_requested";
}

export type ActualSurface = "monitor" | "window";

export interface CloudConsent {
    cloud_derived_chat: boolean;
    cloud_vision:       boolean;
}

export interface SessionStarted {
    actual_surface:        ActualSurface;
    character_id:          string;
    client_session_id:     string;
    conversation_id:       string;
    event_id:              string;
    generation:            number;
    heartbeat_interval_ms: number;
    lease_duration_ms:     number;
    lease_expires_at:      string;
    protocol_version:      "1.0";
    routing_revision:      string;
    screen_session_id:     string;
    type:                  "screen_session_started";
}

export interface SnapshotRequested {
    capture_deadline:  string;
    event_id:          string;
    generation:        number;
    protocol_version:  "1.0";
    request_id:        string;
    requested_at:      string;
    screen_session_id: string;
    source:            Source;
    turn_id:           string;
    type:              "screen_snapshot_requested";
}

export type Source = "explicit_ui" | "natural_language_text" | "natural_language_voice";

export interface SnapshotUploadAccepted {
    event_id:          string;
    generation:        number;
    image_id:          string;
    protocol_version:  "1.0";
    received_at:       string;
    request_id:        string;
    screen_session_id: string;
    type:              "screen_snapshot_upload_accepted";
}

export interface SnapshotUploadMetadata {
    actual_surface:    ActualSurface;
    byte_length:       number;
    captured_at:       string;
    client_session_id: string;
    event_id:          string;
    generation:        number;
    height:            number;
    image_id:          string;
    mime_type:         MIMEType;
    protocol_version:  "1.0";
    request_id:        string;
    screen_session_id: string;
    turn_id:           string;
    type:              "screen_snapshot_upload_metadata";
    width:             number;
}
