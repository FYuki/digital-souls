/**
 * LiveKit Room client の公開契約とtopic定数。
 *
 * 外部へのwire契約・公開API・観測型はここに集約し、
 * 各責務moduleはこの契約だけを共有する。
 */

import type {
  RemoteParticipant,
  RemoteTrack,
  RemoteTrackPublication,
  Room,
} from 'livekit-client'

import type { VoiceSessionEvent } from '../lib/voice-session/generated'
import type { SnapshotRequested } from '../lib/screen-perception/generated'

import type { CoreDeliveryObservation } from './core-delivery-observation'
import type { DecodedReceiptSnapshot } from './decoded-receipt-audit'
import type { MediaObservation } from './media-observer'
import type { NetworkObservation } from './network-observer'
import type { PacketOutputEvidence } from './packet-output-diagnostic'
import type {
  PacketPlaybackObservation,
  PlaybackCompletion,
} from './packet-renderer'
import type { StaleAudioObservation } from './post-gain-monitor'
import type { RetryObservation } from './reconnect-policy'
import type { RetryTimer } from './control'
import type { RtpPacketGap } from './rtp-packet-sequence'
import type { decodePrivateFrame, OutputStopRequest } from './private-contract'

export type PrivateFrame = ReturnType<typeof decodePrivateFrame>

export type RoomObservation = Readonly<{
  transport: 'available' | 'unavailable' | 'idle'
  control: 'available' | 'unavailable'
  audio: 'available' | 'unavailable'
  generation?: number
  failureContext?: Readonly<Record<string, number>>
  failureReason?: string
  recoveryStopped?: boolean
  failureStage?: 'transport' | 'media_decoder' | 'audio_graph' | 'output_clock' | 'renderer' | 'rtp_timeline'
  mediaPacketLoss?: RtpPacketGap & {responseId: string; atMs: number}
  mediaTimelineInterruption?: {responseId: string; atMs: number; reason: 'timestamp_overlap' | 'timestamp_discontinuity'}
  renderedSamples?: number
  playedPrefix?: number
  microphoneFrames?: number
  microphoneSamples?: number
  duplicateTrackFrames?: number
  activeAudioGraphs?: number
  renderedEnergy?: number
  confirmedSegments?: number
  unassignedRenderedSamples?: number
  acknowledgedPlaybackPrefix?: number
  terminalResponseId?: string
  terminalConfirmedAudioSequence?: number
  activeResponseId?: string
  speechStartedAtMs?: number
  localPlaybackStoppedAtMs?: number
  firstPlaybackAtMs?: number
  mediaResponseId?: string
  mediaTrackResponseId?: string
  mediaObservation?: MediaObservation
  mediaCorrelationMissingReason?: 'response_frame_correlation_unavailable'
  packetPlaybackObservation?: PacketPlaybackObservation
  playbackCompletedResponseId?: string
  playbackCompletion?: PlaybackCompletion
  cancelConfirmedAtMs?: number
  networkResponseId?: string
  networkObservation?: NetworkObservation
  networkMeasurementDelivered?: boolean
}>

export type MicrophoneCaptureOptions = Readonly<{
  echoCancellation: boolean
  noiseSuppression: boolean
  channelCount: number
}>

export const DEFAULT_MICROPHONE_CAPTURE_OPTIONS: MicrophoneCaptureOptions = {
  echoCancellation: true,
  noiseSuppression: true,
  channelCount: 1,
}

export const responseIdFromTrackName = (name: string): string | null => {
  const match = /^ds-response-v1:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/.exec(name)
  return match?.[1] ?? null
}

export const PRIVATE_TOPIC = 'digital-souls.livekit-transport.v2'
export const APPLICATION_TOPIC = 'digital-souls.core.v1'
export const SCREEN_TOPIC = 'digital-souls.screen-perception.v1'
export const browserRetryTimer: RetryTimer = {
  now: () => performance.now(),
  schedule: (callback, delayMs) => setTimeout(callback, delayMs),
  cancel: (handle) => clearTimeout(handle),
}

export type DisconnectObservation = Readonly<{reason: number | null; origin: 'sdk' | 'explicit' | 'temporary' | 'transport_failure'}>

export type ConnectionLifecycleObservation = Readonly<{event: 'retry_scheduled' | 'signal_reconnecting' | 'signal_connected'
  | 'reconnecting' | 'reconnected' | 'disconnected' | 'state_sync_requested' | 'state_sync_deferred' | 'authoritative_state' | 'ack_deferred';
  atMs: number; generation: number; retry?: RetryObservation; disconnect?: DisconnectObservation}>

/**
 * 責務ownerからfacadeの横断操作へ戻るための内部契約。
 * 公開APIではなく、module間の結線だけを表す。
 */
export interface RoomClientHost {
  readonly room: Room | null
  readonly sessionId: string | null
  readonly generation: number
  readonly recovering: boolean
  readonly recoveryPending: boolean
  readonly recoverySynchronized: boolean
  readonly syncRequestedGeneration: number | null
  readonly controlAvailable: boolean

  observe(observation: RoomObservation): void
  observeConnection(
    event: ConnectionLifecycleObservation['event'],
    retry?: RetryObservation,
    disconnect?: DisconnectObservation,
  ): void
  failTransport(
    failureStage?: NonNullable<RoomObservation['failureStage']>,
    reason?: unknown,
  ): void
  traceLifecycle(
    direction: 'incoming' | 'outgoing',
    event: VoiceSessionEvent,
  ): void
  receiveCoreEvent(event: VoiceSessionEvent): void
  receiveScreenRequest(event: SnapshotRequested): void
  isProbePublisher(
    participant: RemoteParticipant | undefined,
  ): participant is RemoteParticipant
  stopPlayback(responseId: string, speechStartedAtMs?: number): number
  publishControlEvent(value: VoiceSessionEvent): Promise<void>
  publishMediaMeasurement(
    responseId: string,
    measurement:
      | 'client_track_received'
      | 'client_encoded_received'
      | 'client_audio_decoded'
      | 'playback_started',
    atMs: number,
  ): Promise<void>
  publishPlaybackConfirmation(
    responseId: string,
    continuousPrefix: number,
    responseFinished?: boolean,
    summary?: PlaybackCompletion,
  ): Promise<void>
  publishPlaybackStarted(responseId: string, atMs: number): Promise<void>
  beginStateRecovery(room: Room, awaitingReconnect?: boolean): void
  requestStateSync(room: Room): Promise<void>
  resetControlProbes(): void
  cancelAudioProbe(): void
  acknowledgeRecoveryProbe(probeId: string, generation: number): void
  markResponseStopped(responseId: string): void
  isResponseStopped(responseId: string): boolean
  handleGenerationChanged(generation: number): void
  hasAudioGraphs(): boolean
  handleResponseStarted(responseId: string): void
  noteSuppressedResponse(responseId: string): void
  handleResponseAudioSegment(responseId: string): void
  handleResponseTerminal(responseId: string): void
  handleResponseCancelled(responseId: string): void
}

/**
 * wiring が RoomEvent と decode済みframeを振り分けるためのsink契約。
 */
export interface RoomEventSink {
  readonly generation: number

  isProbePublisher(
    participant: RemoteParticipant | undefined,
  ): participant is RemoteParticipant
  failTransport(
    failureStage?: NonNullable<RoomObservation['failureStage']>,
    reason?: unknown,
  ): void
  observe(observation: RoomObservation): void
  observeConnection(
    event: ConnectionLifecycleObservation['event'],
    retry?: RetryObservation,
    disconnect?: DisconnectObservation,
  ): void
  handleSignalReconnecting(room: Room): void
  handleSignalConnected(): void
  handleReconnecting(room: Room): void
  handleReconnected(room: Room): void
  handleDisconnected(reason: unknown): void
  handleApplicationFrame(room: Room, payload: Uint8Array): void
  handleScreenFrame(payload: Uint8Array): void
  handleOutputStopRequest(room: Room, frame: OutputStopRequest): void
  handlePrivateFrame(
    room: Room,
    frame: Exclude<PrivateFrame, OutputStopRequest>,
    participant: RemoteParticipant | undefined,
  ): void
  handleTrackPublished(
    publication: RemoteTrackPublication,
    participant: RemoteParticipant,
  ): void
  handleTrackSubscribed(
    room: Room,
    track: RemoteTrack,
    publication: RemoteTrackPublication,
    participant: RemoteParticipant,
  ): void
  handleTrackUnsubscribed(
    track: RemoteTrack,
    publication: RemoteTrackPublication,
  ): void
}
