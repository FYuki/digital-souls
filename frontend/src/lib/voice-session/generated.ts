/**
 * transport 非依存の双方向音声セッションイベント契約。音声バイト列はこのイベント契約の外側にある一時 media として扱う。
 */
export interface VoiceSessionEvent {
    event_id:                      string;
    monotonic_timestamp_ms?:       number;
    protocol_version:              "1.0";
    requested_reconnect_grace_ms?: number;
    session_id:                    string;
    type:                          Type;
    reconnect_grace_ms?:           number;
    reason?:                       VoiceSessionEventReason;
    response_id?:                  string;
    speaker?:                      Speaker;
    utterance_id?:                 string;
    decision?:                     Decision;
    final?:                        boolean;
    should_response?:              boolean;
    transcript?:                   string;
    history_turn_id?:              string;
    source_utterance_ids?:         string[];
    text?:                         string;
    text_range?:                   TextRange;
    text_sequence?:                number;
    audio_sequence?:               number;
    last_audio_sequence?:          number;
    last_text_sequence?:           number;
    error_code?:                   string;
    recoverable?:                  boolean;
    last_played_audio_sequence?:   number;
    playback_summary?:             PlaybackSummary;
    response_finished?:            boolean;
    classification?:               Classification;
    user_state?:                   UserState;
    clock_domain?:                 ClockDomain;
    measurement?:                  Measurement;
    network_summary?:              NetworkSummary;
    session_summary?:              SessionSummary;
    timestamp?:                    number | string;
    unit?:                         Unit;
}

export type Classification = "recoverable" | "terminal";

export type ClockDomain = "client_monotonic" | "server_monotonic";

export type Decision = "backchannel" | "take_turn" | "indeterminate";

export type Measurement = "speech_stopped" | "utterance_finalized" | "response_started" | "first_audio_out" | "playback_started" | "client_track_received" | "client_encoded_received" | "client_audio_decoded" | "turn_decision_received" | "cancel_confirmed" | "local_playback_stopped" | "network_summary" | "session_summary";

export interface NetworkSummary {
    downlink: Downlink;
    method:   "browser_audio_rtp_counters_v1";
    uplink:   Uplink;
}

export interface Downlink {
    bytes?:       number;
    lostPackets?: number;
    packets?:     number;
    status:       Status;
    reason?:      DownlinkReason;
}

export type DownlinkReason = "stats_api_unavailable" | "stats_failed" | "stats_timeout" | "stats_unavailable" | "ambiguous_audio_stream" | "invalid_rtp_counters" | "counter_regressed" | "playback_packets_not_yet_reported";

export type Status = "measured" | "missing";

export interface Uplink {
    bytes?:   number;
    packets?: number;
    status:   Status;
    reason?:  DownlinkReason;
}

export interface PlaybackSummary {
    confirmation_observed_at_ms:   number;
    expected_samples:              number;
    first_output_frame:            number;
    first_rtp_timestamp:           number;
    gap_count:                     number;
    gap_samples:                   number;
    input_samples:                 number;
    last_output_end_frame:         number;
    last_rtp_timestamp:            number;
    maximum_gap_samples:           number;
    output_clock_context_time:     number;
    output_clock_performance_time: number;
    packet_count:                  number;
    padding_samples:               number;
    rendered_samples:              number;
    sample_rate:                   number;
}

export type VoiceSessionEventReason = "user_request" | "terminal_error" | "reconnect_timeout" | "privacy" | "disconnect" | "session_ended" | "invalid_audio" | "input_capacity_exceeded" | "barge_in" | "decode_failure";

export interface SessionSummary {
    end_requested:                  boolean;
    microphone_activation_attempts: number;
    mute_attempts:                  number;
    operation_tracking_started:     boolean;
    retry_attempts:                 number;
    sequence:                       number;
}

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

export type Type = "session_start_requested" | "session_started" | "session_muted" | "session_resumed" | "session_ended" | "session_disconnected" | "session_reconnect_requested" | "session_reconnected" | "speech_started" | "speech_stopped" | "turn_decision" | "utterance_finalized" | "utterance_pending" | "utterance_discarded" | "response_started" | "response_delta" | "response_audio_segment" | "response_completed" | "response_cancel_requested" | "response_cancelled" | "response_failed" | "playback_started" | "playback_stopped" | "playback_completed" | "playback_decode_failed" | "error" | "observation";

export type Unit = "millisecond" | "nanosecond";

export type UserState = "listening" | "muted" | "reconnecting" | "ended" | "error";
