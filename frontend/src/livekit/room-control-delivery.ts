/**
 * Core event 受信・ACK・outbox・疎通probeを所有する。
 *
 * Core event の重複除外とACK、application topic のoutbox、
 * playback確認、接続疎通probeの進行状態を一つの所有点に集約する。
 * 音声graph・世代・Room配線の実資源はfacade経由で参照する。
 */

import {
  Track,
  type RemoteParticipant,
  type RemoteTrack,
  type RemoteTrackPublication,
  type Room,
} from 'livekit-client'

import type { VoiceSessionEvent } from '../lib/voice-session/generated'
import { parseVoiceSessionEvent } from '../lib/voice-session/validation'

import type { CoreDeliveryObservation } from './core-delivery-observation'
import { CoreAckOutbox } from './core-ack-outbox'
import {
  AudioAvailabilityProbe,
  type AudioProbeObservation,
} from './audio-probe'
import {
  ControlProbeTracker,
  type ControlProbeObservation,
} from './control-probe'
import {
  BrowserControlOutbox,
  CoreEventReceiver,
  PlaybackConfirmationTracker,
} from './control'
import type { SourceAudioFinished, PlaybackCompletion } from './packet-renderer'
import {
  APPLICATION_TOPIC,
  PRIVATE_TOPIC,
  browserRetryTimer,
  type RoomClientHost,
} from './room-contract'

export class LiveKitRoomControlDelivery {
  private readonly coreEvents = new CoreEventReceiver()
  private coreAckOutbox: CoreAckOutbox | null = null
  private playbackConfirmations: PlaybackConfirmationTracker | null = null
  private controlOutbox: BrowserControlOutbox | null = null
  private finalSummaryAck: { eventId: string; resolve: () => void } | null = null
  private readonly controlProbes = new ControlProbeTracker(browserRetryTimer)
  private readonly clockProbes = new ControlProbeTracker(browserRetryTimer)
  private audioProbe: AudioAvailabilityProbe | null = null
  private coreDeliveryObserver:
    | ((row: CoreDeliveryObservation) => void)
    | undefined

  constructor(private readonly host: RoomClientHost) {}

  get controlAvailable(): boolean {
    return this.controlOutbox !== null
  }

  setCoreDeliveryObserver(
    observer: (row: CoreDeliveryObservation) => void,
  ): void {
    this.coreDeliveryObserver = observer
  }

  resetControlProbes(): void {
    this.controlProbes.reset()
    this.clockProbes.reset()
    this.audioProbe?.cancel()
  }

  cancelAudioProbe(): void {
    this.audioProbe?.cancel()
  }

  clearCoreEvents(): void {
    this.coreEvents.clear()
  }

  clearCoreAck(): void {
    this.coreAckOutbox?.clear()
    this.coreAckOutbox = null
  }

  clearBrowserDelivery(): void {
    this.controlOutbox?.clear()
    this.controlOutbox = null
    this.playbackConfirmations = null
  }

  startBrowserDelivery(sessionId: string, room: Room): void {
    this.clearBrowserDelivery()
    this.coreAckOutbox ??= new CoreAckOutbox(
      async (eventId) => {
        if (this.host.room !== room || this.host.sessionId !== sessionId) {
          throw new Error('Core ACK transport unavailable')
        }
        await room.localParticipant.publishData(
          new TextEncoder().encode(
            JSON.stringify({
              protocol_version: '2.0',
              type: 'ack',
              event_id: eventId,
              generation: this.host.generation,
            }),
          ),
          { reliable: true, topic: PRIVATE_TOPIC },
        )
      },
      browserRetryTimer,
      () => this.host.failTransport(),
      () => this.host.observeConnection('ack_deferred'),
    )
    this.playbackConfirmations = new PlaybackConfirmationTracker(
      sessionId,
      () => Math.floor(performance.now()),
      () => crypto.randomUUID(),
    )
    this.controlOutbox = new BrowserControlOutbox(
      (payload) =>
        room.localParticipant.publishData(payload, {
          reliable: true,
          topic: APPLICATION_TOPIC,
        }),
      () => this.host.failTransport(),
      browserRetryTimer,
    )
  }

  probeControl(): Promise<ControlProbeObservation> {
    const room = this.host.room
    if (room === null || this.controlOutbox === null) {
      return Promise.resolve({
        status: 'unavailable',
        generation: this.host.generation,
        probeId: null,
        sentAtMs: null,
        receivedAtMs: null,
      })
    }
    return this.controlProbes.start(
      this.host.generation,
      async (probeId, generation) => {
        await room.localParticipant.publishData(
          new TextEncoder().encode(
            JSON.stringify({
              protocol_version: '2.0',
              type: 'control_probe',
              probe_id: probeId,
              generation,
            }),
          ),
          { reliable: true, topic: PRIVATE_TOPIC },
        )
      },
    )
  }

  probeClock(): Promise<ControlProbeObservation> {
    const room = this.host.room
    if (
      room === null ||
      this.controlOutbox === null ||
      this.host.recovering ||
      this.host.recoveryPending ||
      this.host.syncRequestedGeneration !== null
    ) {
      return Promise.resolve({
        status: 'unavailable',
        generation: this.host.generation,
        probeId: null,
        sentAtMs: null,
        receivedAtMs: null,
      })
    }
    // 初回connect・状態同期の途中へ診断publishを差し込まない。
    // 復旧判定用probeとはpending状態を分け、通常の疎通指標へ時計診断を混ぜない。
    return this.clockProbes.start(
      this.host.generation,
      async (probeId, generation) => {
        await room.localParticipant.publishData(
          new TextEncoder().encode(
            JSON.stringify({
              protocol_version: '2.0',
              type: 'control_probe',
              probe_id: probeId,
              generation,
              observe_clock: true,
            }),
          ),
          { reliable: true, topic: PRIVATE_TOPIC },
        )
      },
    )
  }

  isAudioProbeReady(): boolean {
    return (
      this.host.room !== null &&
      this.host.sessionId !== null &&
      this.controlOutbox !== null &&
      // 制御の疎通だけではmediaの再接続完了を証明できない。
      !this.host.recovering &&
      !this.host.recoveryPending &&
      this.host.syncRequestedGeneration === null
    )
  }

  probeAudio(): Promise<AudioProbeObservation> {
    const room = this.host.room
    const sessionId = this.host.sessionId
    const generation = this.host.generation
    const probe = new AudioAvailabilityProbe(
      crypto.randomUUID(),
      generation,
      () =>
        this.host.room === room &&
        this.host.sessionId === sessionId &&
        this.host.generation === generation &&
        this.controlOutbox !== null,
      async (frame) => {
        if (room === null) throw new Error('room unavailable')
        await room.localParticipant.publishData(
          new TextEncoder().encode(JSON.stringify(frame)),
          { reliable: true, topic: PRIVATE_TOPIC },
        )
      },
    )
    if (!this.isAudioProbeReady()) probe.cancel('unavailable')
    else if (this.audioProbe !== null) probe.cancel('busy')
    else {
      this.audioProbe = probe
      probe.start()
      void probe.result.then(() => {
        if (this.audioProbe === probe) this.audioProbe = null
      })
    }
    return probe.result
  }

  async publishControlEvent(value: VoiceSessionEvent): Promise<void> {
    this.host.traceLifecycle('outgoing', value)
    const sessionId = this.host.sessionId
    const outbox = this.controlOutbox
    if (sessionId === null || outbox === null) {
      throw new Error('LiveKit Room control channel is not connected')
    }
    const event = parseVoiceSessionEvent(value)
    if (event.session_id !== sessionId) {
      throw new Error(
        'control event session_id does not match the connected session',
      )
    }
    const payload = new TextEncoder().encode(JSON.stringify(event))
    if (
      event.type !== 'observation' ||
      event.measurement !== 'session_summary' ||
      !event.session_summary?.end_requested
    ) {
      await outbox.enqueue({ event }, payload)
      return
    }
    let timer: ReturnType<typeof setTimeout> | undefined
    const acknowledged = new Promise<void>((resolve) => {
      this.finalSummaryAck = { eventId: event.event_id, resolve }
      timer = setTimeout(resolve, 500)
    })
    try {
      await outbox.enqueue({ event }, payload)
      await acknowledged
    } finally {
      if (timer !== undefined) clearTimeout(timer)
      if (this.finalSummaryAck?.eventId === event.event_id) {
        this.finalSummaryAck = null
      }
    }
  }

  async publishPlaybackConfirmation(
    responseId: string,
    continuousPrefix: number,
    responseFinished = false,
    summary?: PlaybackCompletion,
  ): Promise<void> {
    const tracker = this.playbackConfirmations
    const outbox = this.controlOutbox
    if (tracker === null || outbox === null) {
      throw new Error('LiveKit Room is not connected')
    }
    const confirmation = tracker.create(
      responseId,
      continuousPrefix,
      responseFinished,
      summary,
    )
    if (confirmation === null) return
    const payload = new TextEncoder().encode(
      JSON.stringify(confirmation.event),
    )
    await outbox.enqueue(confirmation, payload)
  }

  async publishMediaMeasurement(
    responseId: string,
    measurement:
      | 'client_track_received'
      | 'client_encoded_received'
      | 'client_audio_decoded'
      | 'playback_started',
    atMs: number,
  ): Promise<void> {
    const sessionId = this.host.sessionId
    if (sessionId === null) throw new Error('LiveKit Room is not connected')
    await this.publishControlEvent(
      parseVoiceSessionEvent({
        type: 'observation',
        protocol_version: '2.0',
        event_id: crypto.randomUUID(),
        session_id: sessionId,
        response_id: responseId,
        measurement,
        timestamp: Math.floor(atMs),
        clock_domain: 'client_monotonic',
        unit: 'millisecond',
      }),
    )
  }

  async publishPlaybackStarted(
    responseId: string,
    atMs: number,
  ): Promise<void> {
    await this.publishMediaMeasurement(responseId, 'playback_started', atMs)
  }

  handleAckFrame(frame: { eventId: string; generation: number }): void {
    if (frame.generation !== this.host.generation) return
    const confirmation = this.controlOutbox?.acknowledge(frame.eventId)
    if (this.finalSummaryAck?.eventId === frame.eventId) {
      this.finalSummaryAck.resolve()
      this.finalSummaryAck = null
    }
    if (
      confirmation?.responseId !== undefined &&
      confirmation.continuousPrefix !== undefined
    ) {
      this.host.observe({
        transport: 'available',
        control: 'available',
        audio: 'available',
        acknowledgedPlaybackPrefix: confirmation.continuousPrefix,
      })
    }
  }

  handleAudioProbeFinished(
    frame: {
      probeId: string
      generation: number
      trackSid: string
    } & SourceAudioFinished,
    participant: RemoteParticipant | undefined,
  ): void {
    if (this.host.isProbePublisher(participant)) {
      this.audioProbe?.finish(
        frame.probeId,
        frame.generation,
        frame.trackSid,
        participant.sid,
        frame,
      )
    }
  }

  handleControlProbeAck(
    frame: {
      probeId: string
      generation: number
      serverReceivedAtUs?: number
      serverSentAtUs?: number
    },
    participant: RemoteParticipant | undefined,
  ): void {
    if (this.host.isProbePublisher(participant)) {
      this.host.acknowledgeRecoveryProbe(frame.probeId, frame.generation)
    }
    const clock =
      frame.serverReceivedAtUs === undefined
        ? undefined
        : {
            serverReceivedAtUs: frame.serverReceivedAtUs,
            serverSentAtUs: frame.serverSentAtUs!,
          }
    this.controlProbes.acknowledge(frame.probeId, frame.generation, clock)
    this.clockProbes.acknowledge(frame.probeId, frame.generation, clock)
  }

  noteTrackPublished(
    publication: RemoteTrackPublication,
    participant: RemoteParticipant,
  ): void {
    this.audioProbe?.observeTrack(
      'published',
      publication.trackName,
      this.host.isProbePublisher(participant),
      publication.kind === Track.Kind.Audio,
    )
  }

  noteTrackSubscribed(
    track: RemoteTrack,
    publication: RemoteTrackPublication,
    participant: RemoteParticipant,
  ): void {
    this.audioProbe?.observeTrack(
      'subscribed',
      publication.trackName,
      this.host.isProbePublisher(participant),
      track.kind === Track.Kind.Audio,
    )
  }

  handleProbeTrackSubscribed(
    track: RemoteTrack,
    trackName: string,
    trackSid: string,
    participant: RemoteParticipant,
  ): void {
    if (
      this.host.isProbePublisher(participant) &&
      this.audioProbe?.matchesTrack(trackName)
    ) {
      this.audioProbe.attach(track, trackSid, participant.sid)
    }
  }

  noteTrackUnsubscribed(trackSid: string): void {
    this.audioProbe?.unsubscribe(trackSid)
  }

  async acknowledgeCoreEvent(room: Room, payload: Uint8Array): Promise<void> {
    const { event, duplicate } = this.coreEvents.receive(payload)
    // 重複除外・Controllerの旧応答抑止より前の受信を、本文を含めず診断する。
    if (
      event.response_id !== undefined &&
      (event.type === 'response_started' ||
        event.type === 'response_delta' ||
        event.type === 'response_cancelled')
    ) {
      this.coreDeliveryObserver?.({
        type: event.type,
        sessionId: event.session_id,
        responseId: event.response_id,
        generation: this.host.generation,
        atMs: performance.now(),
        duplicate,
        textCharacters: event.text?.length ?? 0,
        textSequence: event.text_sequence ?? null,
        ...(event.history_turn_id === undefined
          ? {}
          : { historyTurnId: event.history_turn_id }),
      })
    }
    if (!duplicate) {
      this.host.traceLifecycle('incoming', event)
      if (
        event.type === 'response_started' &&
        event.response_id !== undefined &&
        !this.host.isResponseStopped(event.response_id)
      ) {
        this.host.handleResponseStarted(event.response_id)
      }
      if (
        event.type === 'response_started' &&
        event.response_id !== undefined
      ) {
        // response_started直後は、remote trackに割り込み前の音声が残っている場合がある。
        // 次回答の最初の音声セグメントが確定するまで再生graphを復旧しない。
        this.host.noteSuppressedResponse(event.response_id)
      }
      if (
        event.type === 'response_audio_segment' &&
        event.response_id !== undefined
      ) {
        this.host.handleResponseAudioSegment(event.response_id)
      }
      if (
        (event.type === 'response_cancelled' ||
          event.type === 'response_failed' ||
          event.type === 'response_privacy_skipped') &&
        event.response_id !== undefined
      ) {
        this.host.handleResponseTerminal(event.response_id)
      }
      if (event.type === 'response_cancelled' && event.response_id !== undefined) {
        // 取消確認後のRTP snapshotは停止処理を待たせず、完全再生と別の境界で記録する。
        this.host.handleResponseCancelled(event.response_id)
      }
      // 受信・sequence・ACKは記録し、停止済み本文だけを表示先へ再配送しない。
      if (
        !(
          event.type === 'response_delta' &&
          event.response_id !== undefined &&
          this.host.isResponseStopped(event.response_id)
        )
      ) {
        this.host.receiveCoreEvent(event)
      }
    }
    if (this.host.room === room) this.coreAckOutbox?.enqueue(event.event_id)
    if (event.type === 'session_ended') this.host.failTransport()
  }
}
