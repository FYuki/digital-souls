/**
 * LiveKit Room client の公開facade。
 *
 * 公開APIとsession識別の保持だけを担い、応答音声graphは
 * room-audio-graph、世代・復旧はroom-recovery、Core受信・
 * ACK・outboxはroom-control-delivery、RoomEvent結線は
 * room-event-wiring、公開契約はroom-contractが担う。
 */

import {
  Track,
  type RemoteParticipant,
  type RemoteTrack,
  type RemoteTrackPublication,
  type Room,
} from 'livekit-client'

import type { VoiceSessionEvent } from '../lib/voice-session/generated'
import type { SnapshotRequested } from '../lib/screen-perception/generated'
import { parseScreenPerceptionEvent } from '../lib/screen-perception/validation'

import { AUDIO_PROBE_TRACK_PREFIX } from './audio-probe'
import { PacketRenderError } from './packet-renderer'
import type { PlaybackCompletion } from './packet-renderer'
import { RtpPacketSequenceError } from './rtp-packet-sequence'
import type { CoreDeliveryObservation } from './core-delivery-observation'
import type { DecodedReceiptSnapshot } from './decoded-receipt-audit'
import type { PacketOutputEvidence } from './packet-output-diagnostic'
import type { StaleAudioObservation } from './post-gain-monitor'
import type { RetryObservation } from './reconnect-policy'
import type { OutputStopRequest } from './private-contract'
import {
  DEFAULT_MICROPHONE_CAPTURE_OPTIONS,
  responseIdFromTrackName,
  type ConnectionLifecycleObservation,
  type DisconnectObservation,
  type MicrophoneCaptureOptions,
  type PrivateFrame,
  type RoomClientHost,
  type RoomEventSink,
  type RoomObservation,
} from './room-contract'
import { LiveKitRoomAudioGraph } from './room-audio-graph'
import { LiveKitRoomControlDelivery } from './room-control-delivery'
import { LiveKitRoomRecovery } from './room-recovery'
import { createWiredRoom } from './room-event-wiring'

export {
  type ConnectionLifecycleObservation,
  type DisconnectObservation,
  type MicrophoneCaptureOptions,
  type RoomObservation,
}

const KNOWN_FAILURE_REASONS = [
  'event deduplication capacity exceeded',
  'browser control outbox capacity exceeded',
  'output_stop_request_changed',
  'output_stop_graph_missing',
  'output_stop_monitor_missing',
  'output_stop_request_mismatch',
  'output_stop_monitor_unavailable',
  'output_stop_marker_invalid',
  'output_stop_confirmation_timeout',
  'output_stop_monitor_closed',
  'output_stop_observation_invalid',
  'first output media correlation mismatch',
  'full playback metadata does not match source samples',
  'state_sync_timeout',
  'recovery_probe_timeout',
  'RTP packet sequence invalid',
  'invalid packet render interval',
  'invalid RTP timestamp',
  'RTP timeline discontinuity',
  'render beyond completed source',
  'output clock confirmation queue overflow',
  'invalid packet output clock',
  'first output packet mismatch',
  'packet_or_sample_mismatch',
  'pcm_queue_overflow',
  'render_clock_unreconciled',
]

export class LiveKitRoomClient {
  private roomState: Room | null = null
  private sessionState: string | null = null
  private explicitDisconnect = false
  private pendingDisconnectOrigin: DisconnectObservation['origin'] | null =
    null
  private reconnectRequested = false
  private connectionObserver:
    | ((row: ConnectionLifecycleObservation) => void)
    | undefined

  // 責務ownerとevent wiringへ渡す内部結線。公開APIではない。
  private readonly internals: RoomClientHost & RoomEventSink
  private readonly recovery: LiveKitRoomRecovery
  private readonly delivery: LiveKitRoomControlDelivery
  private readonly audioGraph: LiveKitRoomAudioGraph

  constructor(
    private readonly receiveObservation: (observation: RoomObservation) => void,
    private readonly receiveCoreEventCallback: (
      event: VoiceSessionEvent,
    ) => void = () => undefined,
    private readonly microphoneCaptureOptions: MicrophoneCaptureOptions = DEFAULT_MICROPHONE_CAPTURE_OPTIONS,
    private readonly receiveScreenRequestCallback: (
      event: SnapshotRequested,
    ) => void = () => undefined,
  ) {
    this.internals = this.createInternals()
    this.recovery = new LiveKitRoomRecovery(this.internals)
    this.delivery = new LiveKitRoomControlDelivery(this.internals)
    this.audioGraph = new LiveKitRoomAudioGraph(this.internals)
  }

  private get room(): Room | null {
    return this.roomState
  }

  private get sessionId(): string | null {
    return this.sessionState
  }

  private get generation(): number {
    return this.recovery.generation
  }

  private get recovering(): boolean {
    return this.recovery.recovering
  }

  private get recoveryPending(): boolean {
    return this.recovery.recoveryPending
  }

  private get recoverySynchronized(): boolean {
    return this.recovery.recoverySynchronized
  }

  private get syncRequestedGeneration(): number | null {
    return this.recovery.syncRequestedGenerationValue
  }

  private get controlAvailable(): boolean {
    return this.delivery.controlAvailable
  }

  private receiveCoreEvent(event: VoiceSessionEvent): void {
    this.receiveCoreEventCallback(event)
  }

  private receiveScreenRequest(event: SnapshotRequested): void {
    this.receiveScreenRequestCallback(event)
  }

  private traceLifecycle(
    direction: 'incoming' | 'outgoing',
    event: VoiceSessionEvent,
  ): void {
    if (
      ![
        'user_text_submitted',
        'user_input_result',
        'speech_started',
        'speech_stopped',
        'turn_decision',
        'response_started',
        'response_cancel_requested',
        'response_cancelled',
        'response_completed',
        'response_failed',
        'utterance_discarded',
      ].includes(event.type)
    ) {
      return
    }
    // devだけで取消元を相関する。本文・音声・tokenは送らない。
    ;(
      import.meta as ImportMeta & {
        hot?: { send(event: string, data: unknown): void }
      }
    ).hot?.send('voice:lifecycle', {
      direction,
      type: event.type,
      event_id: event.event_id,
      response_id: event.response_id,
      utterance_id: event.utterance_id,
      input_event_id: event.input_event_id,
      reason: event.reason,
      decision: event.decision,
      status: event.status,
    })
  }

  private observe(observation: RoomObservation): void {
    // 下りの状態通知や再生継続だけでは、復旧後の上りの疎通を確認できない。
    this.receiveObservation(
      this.recovery.recoveryPending && observation.transport === 'available'
        ? { ...observation, transport: 'unavailable', control: 'unavailable' }
        : observation,
    )
  }

  async connect(url: string, token: string, sessionId: string): Promise<void> {
    this.recovery.beginConnect()
    this.delivery.resetControlProbes()
    if (this.sessionState !== sessionId) {
      this.delivery.clearCoreAck()
      this.audioGraph.onSessionChanged()
    }
    if (this.roomState === null) this.roomState = this.createRoom()
    const shouldSynchronize =
      this.reconnectRequested && this.sessionState === sessionId
    this.recovery.clearRecoveryProbe()
    this.recovery.recoveryPending = shouldSynchronize
    this.explicitDisconnect = false
    this.pendingDisconnectOrigin = null
    this.sessionState = sessionId
    this.delivery.startBrowserDelivery(sessionId, this.roomState)
    await this.roomState.connect(url, token)
    this.recovery.recovering = false
    if (shouldSynchronize) {
      await this.recovery.requestStateSync(this.roomState)
      this.recovery.confirmRecovery(this.roomState)
    }
    this.reconnectRequested = false
    this.observe({
      transport: 'available',
      control: 'available',
      audio: 'unavailable',
    })
  }

  setConnectionObserver(
    observer: (row: ConnectionLifecycleObservation) => void,
  ): void {
    this.connectionObserver = observer
  }

  private observeConnection(
    event: ConnectionLifecycleObservation['event'],
    retry?: RetryObservation,
    disconnect?: DisconnectObservation,
  ): void {
    this.connectionObserver?.({
      event,
      atMs: performance.now(),
      generation: this.generation,
      ...(retry ? { retry } : {}),
      ...(disconnect ? { disconnect } : {}),
    })
  }

  setCoreDeliveryObserver(
    observer: (row: CoreDeliveryObservation) => void,
  ): void {
    this.delivery.setCoreDeliveryObserver(observer)
  }

  setDecodedReceiptObserver(
    observer: (row: DecodedReceiptSnapshot) => void,
  ): void {
    this.audioGraph.setDecodedReceiptObserver(observer)
  }

  setStaleAudioObserver(
    observer: (row: StaleAudioObservation) => void,
  ): void {
    this.audioGraph.setStaleAudioObserver(observer)
  }

  setPacketOutputObserver(
    observer: (row: PacketOutputEvidence) => void,
  ): void {
    this.audioGraph.setPacketOutputObserver(observer)
  }

  probeControl() {
    return this.delivery.probeControl()
  }

  probeClock() {
    return this.delivery.probeClock()
  }

  isAudioProbeReady(): boolean {
    return this.delivery.isAudioProbeReady()
  }

  probeAudio() {
    return this.delivery.probeAudio()
  }

  private isProbePublisher(
    participant: RemoteParticipant | undefined,
  ): participant is RemoteParticipant {
    return (
      this.sessionId !== null &&
      participant !== undefined &&
      !!participant.sid &&
      participant.identity.startsWith('character-') &&
      participant.identity.endsWith('-' + this.sessionId)
    )
  }

  async publishMicrophone(stream?: MediaStream): Promise<string> {
    if (this.room === null) throw new Error('LiveKit Room is not connected')
    if (stream !== undefined) {
      const track = stream.getAudioTracks()[0]
      if (track === undefined)
        throw new Error('Microphone stream has no audio track')
      const publication = await this.room.localParticipant.publishTrack(
        track,
        {
          source: Track.Source.Microphone,
        },
      )
      return publication.trackSid
    }
    const publication = await this.room.localParticipant.setMicrophoneEnabled(
      true,
      { ...this.microphoneCaptureOptions },
    )
    if (publication === undefined)
      throw new Error('Microphone publication is unavailable')
    return publication.trackSid
  }

  async muteMicrophone(): Promise<void> {
    if (this.room === null) throw new Error('LiveKit Room is not connected')
    const publication = this.room.localParticipant.getTrackPublication(
      Track.Source.Microphone,
    )
    if (publication?.track !== undefined) {
      await this.room.localParticipant.unpublishTrack(publication.track, false)
      return
    }
    await this.room.localParticipant.setMicrophoneEnabled(false)
  }

  async publishControlEvent(value: VoiceSessionEvent): Promise<void> {
    await this.delivery.publishControlEvent(value)
  }

  private async publishPlaybackConfirmation(
    responseId: string,
    continuousPrefix: number,
    responseFinished = false,
    summary?: PlaybackCompletion,
  ): Promise<void> {
    await this.delivery.publishPlaybackConfirmation(
      responseId,
      continuousPrefix,
      responseFinished,
      summary,
    )
  }

  private async publishMediaMeasurement(
    responseId: string,
    measurement:
      | 'client_track_received'
      | 'client_encoded_received'
      | 'client_audio_decoded'
      | 'playback_started',
    atMs: number,
  ): Promise<void> {
    await this.delivery.publishMediaMeasurement(
      responseId,
      measurement,
      atMs,
    )
  }

  private async publishPlaybackStarted(
    responseId: string,
    atMs: number,
  ): Promise<void> {
    await this.delivery.publishPlaybackStarted(responseId, atMs)
  }

  stopPlayback(responseId: string, speechStartedAtMs?: number): number {
    return this.audioGraph.stopPlayback(responseId, speechStartedAtMs)
  }

  private async observeNetwork(
    responseId: string,
    key: string | undefined,
    generation: number,
    expectedPackets: number,
    boundary?: 'response_cancelled',
  ): Promise<void> {
    await this.audioGraph.observeNetwork(
      responseId,
      key,
      generation,
      expectedPackets,
      boundary,
    )
  }

  disconnect(): void {
    this.delivery.resetControlProbes()
    this.explicitDisconnect = true
    this.pendingDisconnectOrigin = 'explicit'
    this.reconnectRequested = false
    this.sessionState = null
    this.roomState?.disconnect()
    void this.closeAudioGraph()
    this.observe({
      transport: 'idle',
      control: 'unavailable',
      audio: 'unavailable',
    })
  }

  temporaryDisconnect(): void {
    this.delivery.resetControlProbes()
    this.reconnectRequested = true
    this.pendingDisconnectOrigin = 'temporary'
    this.room?.disconnect()
    void this.closeAudioGraph()
    this.observe({
      transport: 'unavailable',
      control: 'unavailable',
      audio: 'unavailable',
    })
  }

  private createRoom(): Room {
    return createWiredRoom(this.internals)
  }

  private handleSignalReconnecting(room: Room): void {
    this.observeConnection('signal_reconnecting')
    this.recovery.beginStateRecovery(room)
  }

  private handleSignalConnected(): void {
    this.observeConnection('signal_connected')
  }

  private handleReconnecting(room: Room): void {
    this.observeConnection('reconnecting')
    this.recovery.beginStateRecovery(room)
    this.delivery.resetControlProbes()
    this.delivery.clearBrowserDelivery()
    this.observe({
      transport: 'unavailable',
      control: 'unavailable',
      audio: 'unavailable',
    })
  }

  private handleReconnected(room: Room): void {
    this.recovery.recovering = false
    this.observeConnection('reconnected')
    const sessionId = this.sessionId
    if (sessionId !== null) this.delivery.startBrowserDelivery(sessionId, room)
    if (!this.recovery.recoverySynchronized) {
      void this.recovery
        .requestStateSync(room)
        .catch(() => this.failTransport())
    }
    this.recovery.confirmRecovery(room)
  }

  private handleDisconnected(reason: unknown): void {
    this.recovery.beginDisconnect()
    // SDK例外本文やtokenを含めず、数値の理由とアプリ側の切断起点だけを残す。
    const origin = this.pendingDisconnectOrigin ?? 'sdk'
    this.pendingDisconnectOrigin = null
    this.observeConnection('disconnected', undefined, {
      reason:
        typeof reason === 'number' && Number.isSafeInteger(reason)
          ? reason
          : null,
      origin,
    })
    this.delivery.resetControlProbes()
    if (!this.explicitDisconnect && this.sessionId !== null) {
      this.reconnectRequested = true
    }
    this.delivery.clearBrowserDelivery()
    void this.closeAudioGraph()
    this.observe({
      transport: this.explicitDisconnect ? 'idle' : 'unavailable',
      control: 'unavailable',
      audio: 'unavailable',
      // SDKのDisconnectedは自動再接続の終了。一時切断は呼び出し元が再接続する。
      ...(!this.explicitDisconnect && origin !== 'temporary'
        ? { recoveryStopped: true }
        : {}),
    })
    this.explicitDisconnect = false
  }

  private handleApplicationFrame(room: Room, payload: Uint8Array): void {
    void this.delivery
      .acknowledgeCoreEvent(room, payload)
      .catch((error) => this.failTransport('transport', error))
  }

  private handleScreenFrame(payload: Uint8Array): void {
    try {
      const event = parseScreenPerceptionEvent(
        JSON.parse(new TextDecoder().decode(payload)) as unknown,
      )
      if (event.type !== 'screen_snapshot_requested')
        throw new Error('invalid screen event')
      this.receiveScreenRequest(event)
    } catch {
      this.failTransport()
    }
  }

  private handleOutputStopRequest(
    room: Room,
    frame: OutputStopRequest,
  ): void {
    void this.audioGraph
      .confirmOutputStop(room, frame)
      .catch((error) => this.failTransport('output_clock', error))
  }

  private handlePrivateFrame(
    room: Room,
    frame: Exclude<PrivateFrame, OutputStopRequest>,
    participant: RemoteParticipant | undefined,
  ): void {
    if (frame.type === 'audio_probe_finished') {
      this.delivery.handleAudioProbeFinished(frame, participant)
      return
    }
    if (frame.type === 'control_probe_ack') {
      this.delivery.handleControlProbeAck(frame, participant)
      return
    }
    if (frame.type === 'authoritative_state') {
      this.recovery.handleAuthoritativeState(room, frame)
      return
    }
    if (frame.type === 'ack') {
      this.delivery.handleAckFrame(frame)
      return
    }
    if (frame.type === 'response_audio_finished') {
      this.audioGraph.handleAudioFinished(frame)
      return
    }
    if (frame.type === 'logical_audio_segment') {
      this.audioGraph.handleLogicalAudioSegment(frame)
      return
    }
    if (
      frame.type === 'microphone_observation' &&
      frame.generation === this.generation
    ) {
      this.observe({
        transport: 'available',
        control: 'available',
        audio: 'available',
        microphoneFrames: frame.frameCount,
        microphoneSamples: frame.sampleCount,
      })
    }
  }

  private handleTrackPublished(
    publication: RemoteTrackPublication,
    participant: RemoteParticipant,
  ): void {
    this.delivery.noteTrackPublished(publication, participant)
  }

  private handleTrackSubscribed(
    room: Room,
    track: RemoteTrack,
    publication: RemoteTrackPublication,
    participant: RemoteParticipant,
  ): void {
    this.delivery.noteTrackSubscribed(track, publication, participant)
    if (track.kind !== Track.Kind.Audio) return
    const key = publication.trackSid
    if (publication.trackName.startsWith(AUDIO_PROBE_TRACK_PREFIX)) {
      this.delivery.handleProbeTrackSubscribed(
        track,
        publication.trackName,
        key,
        participant,
      )
      return
    }
    const responseId = responseIdFromTrackName(publication.trackName)
    if (responseId === null) return
    this.audioGraph.handleResponseTrackSubscribed(
      room,
      track,
      key,
      responseId,
    )
  }

  private handleTrackUnsubscribed(
    _track: RemoteTrack,
    publication: RemoteTrackPublication,
  ): void {
    const key = publication.trackSid
    this.delivery.noteTrackUnsubscribed(key)
    this.audioGraph.handleTrackUnsubscribed(key)
  }

  // 以下はowner間の横断操作。責務ownerからinternals経由でのみ到達する。

  private resetControlProbes(): void {
    this.delivery.resetControlProbes()
  }

  private cancelAudioProbe(): void {
    this.delivery.cancelAudioProbe()
  }

  private acknowledgeRecoveryProbe(probeId: string, generation: number): void {
    this.recovery.acknowledgeRecovery(probeId, generation)
  }

  private markResponseStopped(responseId: string): void {
    this.audioGraph.markStopped(responseId)
  }

  private isResponseStopped(responseId: string): boolean {
    return this.audioGraph.isStopped(responseId)
  }

  private handleGenerationChanged(generation: number): void {
    this.audioGraph.handleGenerationChanged(generation)
  }

  private hasAudioGraphs(): boolean {
    return this.audioGraph.hasGraphs()
  }

  private handleResponseStarted(responseId: string): void {
    this.audioGraph.handleResponseStarted(responseId)
  }

  private noteSuppressedResponse(responseId: string): void {
    this.audioGraph.noteSuppressedResponse(responseId)
  }

  private handleResponseAudioSegment(responseId: string): void {
    this.audioGraph.handleResponseAudioSegment(responseId)
  }

  private handleResponseTerminal(responseId: string): void {
    this.audioGraph.handleResponseTerminal(responseId)
  }

  private handleResponseCancelled(responseId: string): void {
    this.audioGraph.handleResponseCancelled(responseId)
  }

  private beginStateRecovery(room: Room, awaitingReconnect = true): void {
    this.recovery.beginStateRecovery(room, awaitingReconnect)
  }

  private requestStateSync(room: Room): Promise<void> {
    return this.recovery.requestStateSync(room)
  }

  private failTransport(
    failureStage: NonNullable<RoomObservation['failureStage']> = 'transport',
    reason?: unknown,
  ): void {
    // 任意の例外本文を外へ渡さず、内部の固定エラー名だけを診断に残す。
    const message = reason instanceof Error ? reason.message : reason
    const failureReason =
      typeof message === 'string' && KNOWN_FAILURE_REASONS.includes(message)
        ? message
        : 'unclassified'

    // 切断イベントが先に画面状態を破棄しても、開発環境では固定の理由だけを残す。
    ;(
      import.meta as ImportMeta & {
        hot?: { send(event: string, data: unknown): void }
      }
    ).hot?.send('voice:failure', { stage: failureStage, reason: failureReason })
    this.pendingDisconnectOrigin ??= 'transport_failure'
    this.room?.disconnect()
    void this.closeAudioGraph()
    this.observe({
      transport: 'unavailable',
      control: 'unavailable',
      audio: 'unavailable',
      failureStage,
      failureReason,
      recoveryStopped: true,
      ...(reason instanceof PacketRenderError ||
      reason instanceof RtpPacketSequenceError
        ? { failureContext: reason.context }
        : {}),
    })
  }

  /** 責務ownerとRoomEvent結線へ渡す非公開adapter。facadeの公開面は増やさない。 */
  private createInternals(): RoomClientHost & RoomEventSink {
    const client = this
    return {
      get room() {
        return client.room
      },
      get sessionId() {
        return client.sessionId
      },
      get generation() {
        return client.generation
      },
      get recovering() {
        return client.recovering
      },
      get recoveryPending() {
        return client.recoveryPending
      },
      get recoverySynchronized() {
        return client.recoverySynchronized
      },
      get syncRequestedGeneration() {
        return client.syncRequestedGeneration
      },
      get controlAvailable() {
        return client.controlAvailable
      },
      observe: (observation) => client.observe(observation),
      observeConnection: (event, retry, disconnect) =>
        client.observeConnection(event, retry, disconnect),
      failTransport: (failureStage, reason) =>
        client.failTransport(failureStage, reason),
      traceLifecycle: (direction, event) =>
        client.traceLifecycle(direction, event),
      receiveCoreEvent: (event) => client.receiveCoreEvent(event),
      receiveScreenRequest: (event) => client.receiveScreenRequest(event),
      isProbePublisher(participant): participant is RemoteParticipant {
        return client.isProbePublisher(participant)
      },
      stopPlayback: (responseId, speechStartedAtMs) =>
        client.stopPlayback(responseId, speechStartedAtMs),
      publishControlEvent: (value) => client.publishControlEvent(value),
      publishMediaMeasurement: (responseId, measurement, atMs) =>
        client.publishMediaMeasurement(responseId, measurement, atMs),
      publishPlaybackConfirmation: (
        responseId,
        continuousPrefix,
        responseFinished,
        summary,
      ) =>
        client.publishPlaybackConfirmation(
          responseId,
          continuousPrefix,
          responseFinished,
          summary,
        ),
      publishPlaybackStarted: (responseId, atMs) =>
        client.publishPlaybackStarted(responseId, atMs),
      beginStateRecovery: (room, awaitingReconnect) =>
        client.beginStateRecovery(room, awaitingReconnect),
      requestStateSync: (room) => client.requestStateSync(room),
      resetControlProbes: () => client.resetControlProbes(),
      cancelAudioProbe: () => client.cancelAudioProbe(),
      acknowledgeRecoveryProbe: (probeId, generation) =>
        client.acknowledgeRecoveryProbe(probeId, generation),
      markResponseStopped: (responseId) =>
        client.markResponseStopped(responseId),
      isResponseStopped: (responseId) => client.isResponseStopped(responseId),
      handleGenerationChanged: (generation) =>
        client.handleGenerationChanged(generation),
      hasAudioGraphs: () => client.hasAudioGraphs(),
      handleResponseStarted: (responseId) =>
        client.handleResponseStarted(responseId),
      noteSuppressedResponse: (responseId) =>
        client.noteSuppressedResponse(responseId),
      handleResponseAudioSegment: (responseId) =>
        client.handleResponseAudioSegment(responseId),
      handleResponseTerminal: (responseId) =>
        client.handleResponseTerminal(responseId),
      handleResponseCancelled: (responseId) =>
        client.handleResponseCancelled(responseId),
      handleSignalReconnecting: (room) => client.handleSignalReconnecting(room),
      handleSignalConnected: () => client.handleSignalConnected(),
      handleReconnecting: (room) => client.handleReconnecting(room),
      handleReconnected: (room) => client.handleReconnected(room),
      handleDisconnected: (reason) => client.handleDisconnected(reason),
      handleApplicationFrame: (room, payload) =>
        client.handleApplicationFrame(room, payload),
      handleScreenFrame: (payload) => client.handleScreenFrame(payload),
      handleOutputStopRequest: (room, frame) =>
        client.handleOutputStopRequest(room, frame),
      handlePrivateFrame: (room, frame, participant) =>
        client.handlePrivateFrame(room, frame, participant),
      handleTrackPublished: (publication, participant) =>
        client.handleTrackPublished(publication, participant),
      handleTrackSubscribed: (room, track, publication, participant) =>
        client.handleTrackSubscribed(room, track, publication, participant),
      handleTrackUnsubscribed: (track, publication) =>
        client.handleTrackUnsubscribed(track, publication),
    }
  }

  private async closeAudioGraph(): Promise<void> {
    this.audioGraph.closeAudioGraphState()
    this.delivery.clearCoreEvents()
    this.audioGraph.clearOutputState()
    this.recovery.clearRecoveryState()
    this.delivery.clearCoreAck()
    this.delivery.clearBrowserDelivery()
    await this.audioGraph.disposeAudioContext()
  }
}
