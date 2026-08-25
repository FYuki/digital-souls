/**
 * transport 非依存の双方向音声セッションイベント契約。音声バイト列はこのイベント契約の外側にある一時 media として扱う。
 */
export interface VoiceSessionEvent {
    event_id:               string;
    monotonic_timestamp_ms: number;
    protocol_version:       "1.0";
    reconnect_grace_ms?:    number;
    session_id:             string;
    type:                   Type;
    reason?:                Reason;
    reconnect_deadline_ms?: number;
    speaker?:               Speaker;
    utterance_id?:          string;
    sequence?:              number;
    last_sequence?:         number;
    should_response?:       boolean;
    transcript?:            string;
    response_id?:           string;
    source_utterance_ids?:  string[];
    text?:                  string;
    text_range?:            TextRange;
    error_code?:            string;
    recoverable?:           boolean;
    last_played_sequence?:  number;
    classification?:        Classification;
    user_state?:            UserState;
    measurement?:           Measurement;
}

export type Classification = "recoverable" | "terminal";

export type Measurement = "speech_stopped" | "utterance_finalized" | "response_started" | "first_audio_out" | "playback_started";

export type Reason = "user_request" | "terminal_error" | "reconnect_timeout" | "privacy" | "disconnect" | "session_ended" | "invalid_audio" | "barge_in" | "decode_failure";

export interface Speaker {
    character_id?:  string;
    participant_id: string;
    role:           Role;
}

export type Role = "user" | "character";

/**
 * 生成本文を Unicode code point の半開区間 [start, end) で指す。0 <= start <= end を満たす。
 */
export interface TextRange {
    end:   number;
    start: number;
}

export type Type = "session_start_requested" | "session_started" | "session_muted" | "session_resumed" | "session_ended" | "session_disconnected" | "session_reconnect_requested" | "session_reconnected" | "speech_started" | "utterance_audio_chunk" | "speech_stopped" | "utterance_finalized" | "utterance_pending" | "utterance_discarded" | "response_started" | "response_delta" | "response_audio_chunk" | "response_completed" | "response_cancel_requested" | "response_cancelled" | "response_failed" | "playback_started" | "playback_stopped" | "playback_completed" | "playback_decode_failed" | "error" | "observation";

export type UserState = "listening" | "muted" | "reconnecting" | "ended" | "error";
