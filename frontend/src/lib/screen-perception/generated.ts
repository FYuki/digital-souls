/**
 * 画面共有の制御metadata契約。画像本文、質問本文、Vision観測本文、対象名は含めない。
 */
export interface ScreenPerceptionEvent {
    chat_destination?:           ChatDestination;
    client_session_id?:          string;
    event_id:                    string;
    limits?:                     Limits;
    protocol_version:            "1.0";
    routing_revision?:           string;
    type:                        Type;
    vision_destination?:         VisionDestination;
    actual_surface?:             ActualSurface;
    character_id?:               string;
    cloud_consent?:              CloudConsent;
    conversation_id?:            string;
    generation?:                 number;
    requested_surface?:          ActualSurface;
    heartbeat_interval_ms?:      number;
    lease_duration_ms?:          number;
    lease_expires_at?:           string;
    screen_session_id?:          null | string;
    reason?:                     Reason;
    capture_deadline?:           string;
    request_id?:                 string;
    requested_at?:               string;
    source?:                     Source;
    turn_id?:                    string;
    byte_length?:                number;
    captured_at?:                string;
    height?:                     number;
    image_id?:                   string;
    mime_type?:                  MIMEType;
    width?:                      number;
    received_at?:                string;
    capture_state?:              CaptureState;
    last_recognized_capture_at?: null | string;
    reason_code?:                ScreenPerceptionSchema | null;
    recognition_state?:          RecognitionState;
    recoverable?:                boolean;
    stage?:                      Stage;
}

export type ActualSurface = "monitor" | "window";

export type CaptureState = "off" | "selecting" | "active" | "unavailable" | "unsupported";

export type ChatDestination = "local" | "cloud";

export interface CloudConsent {
    cloud_derived_chat: boolean;
    cloud_vision:       boolean;
}

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

export type Reason = "user_off" | "target_change" | "conversation_change" | "character_change" | "capture_ended" | "pagehide" | "backend_disconnect" | "consent_revoked" | "lease_expired" | "routing_changed" | "backend_restart";

export type ScreenPerceptionSchema = "api_unavailable" | "insecure_context" | "transient_activation_required" | "capture_not_allowed" | "no_capture_source" | "capture_os_error" | "picker_cancelled" | "surface_mismatch" | "browser_surface_rejected" | "surface_unknown" | "video_track_invalid" | "frame_unavailable" | "image_type_invalid" | "image_too_large" | "image_decode_failed" | "session_not_found" | "session_revoked" | "session_expired" | "generation_mismatch" | "context_mismatch" | "request_not_found" | "request_expired" | "duplicate_request" | "cloud_consent_required" | "routing_changed" | "vision_unconfigured" | "vision_unsupported" | "vision_timeout" | "vision_unavailable" | "vision_invalid_response" | "request_cancelled" | "backend_unavailable";

export type RecognitionState = "idle" | "snapshot_requested" | "capturing" | "uploading" | "recognizing" | "composing" | "succeeded" | "failed";

export type Source = "explicit_ui" | "natural_language_text" | "natural_language_voice";

export type Stage = "support" | "picker" | "capture" | "session" | "upload" | "vision" | "chat";

export type Type = "screen_routing_disclosed" | "screen_session_start_requested" | "screen_session_started" | "screen_session_heartbeat" | "screen_session_heartbeat_accepted" | "screen_session_revoke_requested" | "screen_session_revoked" | "screen_snapshot_requested" | "screen_snapshot_upload_metadata" | "screen_snapshot_upload_accepted" | "screen_status" | "screen_error";

export type VisionDestination = "local" | "cloud" | "unconfigured";
