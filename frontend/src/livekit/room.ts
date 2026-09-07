import {DecodedReceiptAudit, type DecodedReceiptSnapshot} from './decoded-receipt-audit'
import type {CoreDeliveryObservation} from './core-delivery-observation'
import {postGainAuditSource} from './post-gain-audit'
import {PostGainAudioMonitor, type StaleAudioObservation} from './post-gain-monitor'
import {StateSyncRequest} from './state-sync-request'
import {CoreAckOutbox} from './core-ack-outbox'
import {VoiceReconnectPolicy, type RetryObservation} from './reconnect-policy'
import {AudioAvailabilityProbe, AUDIO_PROBE_TRACK_PREFIX, type AudioProbeObservation} from './audio-probe'
import { ControlProbeTracker, type ControlProbeObservation } from './control-probe'
import {
  Room,
  RoomEvent,
  Track,
  type RemoteTrack,
  type RemoteTrackPublication,
  type RemoteParticipant,
} from 'livekit-client'

import type { VoiceSessionEvent } from '../lib/voice-session/generated'
import { parseVoiceSessionEvent } from '../lib/voice-session/validation'
import type { SnapshotRequested } from '../lib/screen-perception/generated'
import { parseScreenPerceptionEvent } from '../lib/screen-perception/validation'

import {
  PlaybackEvidenceController,
  type SegmentMetadata,
} from './playback'
import {
  BrowserControlOutbox,
  CoreEventReceiver,
  PlaybackConfirmationTracker,
  type RetryTimer,
} from './control'
import { decodePrivateFrame } from './private-contract'
import { packetRendererSource, PacketOutputTracker, PacketRenderError, type PacketRenderInterval, type PacketPlaybackObservation, type SourceAudioFinished, type PlaybackCompletion } from './packet-renderer'
import { RemoteMediaObserver, type MediaObservation, type DecodedAudioPacket } from './media-observer'
import {PacketOutputDiagnostic, type PacketOutputEvidence} from './packet-output-diagnostic'
import {RtpPacketSequence, RtpPacketSequenceError, type RtpPacketGap} from './rtp-packet-sequence'
import { RtpNetworkObserver, type NetworkObservation } from './network-observer'

export type RoomObservation = Readonly<{
  transport: 'available' | 'unavailable' | 'idle'
  control: 'available' | 'unavailable'
  audio: 'available' | 'unavailable'
  generation?: number
  failureContext?: Readonly<Record<string, number>>
  failureReason?: string
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
}>

export type MicrophoneCaptureOptions = Readonly<{
  echoCancellation: boolean
  noiseSuppression: boolean
  channelCount: number
}>

const DEFAULT_MICROPHONE_CAPTURE_OPTIONS: MicrophoneCaptureOptions = {
  echoCancellation: true,
  noiseSuppression: true,
  channelCount: 1,
}

const responseIdFromTrackName = (name: string): string | null => {
  const match = /^ds-response-v1:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/.exec(name)
  return match?.[1] ?? null
}

const PRIVATE_TOPIC = 'digital-souls.livekit-transport.v1'
const APPLICATION_TOPIC = 'digital-souls.core.v1'
const SCREEN_TOPIC = 'digital-souls.screen-perception.v1'
const browserRetryTimer: RetryTimer = {
  now: () => performance.now(),
  schedule: (callback, delayMs) => setTimeout(callback, delayMs),
  cancel: (handle) => clearTimeout(handle),
}

export type DisconnectObservation = Readonly<{reason: number | null; origin: 'sdk' | 'explicit' | 'temporary' | 'transport_failure'}>

export type ConnectionLifecycleObservation = Readonly<{event: 'retry_scheduled' | 'signal_reconnecting' | 'signal_connected'
  | 'reconnecting' | 'reconnected' | 'disconnected' | 'state_sync_requested' | 'state_sync_deferred' | 'authoritative_state' | 'ack_deferred';
  atMs: number; generation: number; retry?: RetryObservation; disconnect?: DisconnectObservation}>

export class LiveKitRoomClient {
  private recovering = false
  private recoverySynchronized = false
  private stateSyncRequest: StateSyncRequest | null = null
  private syncRequestedGeneration: number | null = null
  private coreAckOutbox: CoreAckOutbox | null = null
  private connectionObserver: ((row: ConnectionLifecycleObservation) => void) | undefined
  private audioProbe: AudioAvailabilityProbe | null = null
  private readonly controlProbes = new ControlProbeTracker(browserRetryTimer)
  private readonly clockProbes = new ControlProbeTracker(browserRetryTimer)
  private room: Room | null = null
  private audioContext: AudioContext | null = null
  private workletReady: Promise<void> | null = null
  private generation = 0
  private audioGraphResetVersion = 0
  private audioGraphResetTask: Promise<void> = Promise.resolve()
  private readonly subscriptions = new Set<string>()
  private readonly subscribedTracks = new Map<string, RemoteTrack>()
  private readonly pendingMetadata: SegmentMetadata[] = []
  private readonly pendingFinishes = new Map<string, SourceAudioFinished>()
  private readonly trackResponses = new Map<string, string>()
  private readonly trackMediaEvidence = new Map<string, MediaObservation>()
  private readonly stoppedResponses = new Set<string>()
  private latestResponseId: string | null = null
  private readonly audioGraphs = new Map<string, {
    responseId: string
    outputTracker: PacketOutputTracker
    outputTimer: ReturnType<typeof setInterval>
    firstPacket?: DecodedAudioPacket
    audit?: PostGainAudioMonitor
    packetDiagnostic?: PacketOutputDiagnostic
    packetSequence: RtpPacketSequence
    worklet: AudioWorkletNode
    outputGain: GainNode
    playbackElement: HTMLAudioElement
    suspended: boolean
  }>()
  private readonly audioAuditDisposals = new Set<Promise<void>>()
  private coreDeliveryObserver: ((row: CoreDeliveryObservation) => void) | undefined
  private staleAudioObserver: ((row: StaleAudioObservation) => void) | undefined
  private readonly networkObserver = new RtpNetworkObserver()
  private readonly receiptAudits = new Map<string, DecodedReceiptAudit>()
  private receiptObserver: ((row: DecodedReceiptSnapshot) => void) | undefined
  private readonly mediaObservers = new Map<string, RemoteMediaObserver>()
  private duplicateTrackFrames = 0
  private readonly playbackStartedResponses = new Set<string>()
  private readonly firstOutputTimes = new Map<string, number>()
  private readonly playback: PlaybackEvidenceController
  private readonly coreEvents = new CoreEventReceiver()
  private playbackConfirmations: PlaybackConfirmationTracker | null = null
  private controlOutbox: BrowserControlOutbox | null = null
  private sessionId: string | null = null
  private explicitDisconnect = false
  private pendingDisconnectOrigin: DisconnectObservation['origin'] | null = null
  private reconnectRequested = false
  private suppressedResponseId: string | null = null
  private suppressedLastPlayedAudioSequence = 0
  private pendingPlaybackResponseId: string | null = null

  constructor(
    private readonly observe: (observation: RoomObservation) => void,
    private readonly receiveCoreEvent: (event: VoiceSessionEvent) => void = () => undefined,
    private readonly microphoneCaptureOptions: MicrophoneCaptureOptions = DEFAULT_MICROPHONE_CAPTURE_OPTIONS,
    private readonly receiveScreenRequest: (event: SnapshotRequested) => void = () => undefined,
  ) {
    this.playback = new PlaybackEvidenceController(0, (evidence) => {
      const firstPlaybackAtMs = this.firstOutputTimes.get(evidence.responseId)
      this.observe({
        transport: 'available',
        control: 'available',
        audio: 'available',
        renderedSamples: evidence.renderedSamples,
        playedPrefix: evidence.continuousPrefix,
        duplicateTrackFrames: this.duplicateTrackFrames,
        activeAudioGraphs: [...this.audioGraphs.values()].filter(graph => !graph.suspended).length,
        renderedEnergy: evidence.renderedEnergy,
        confirmedSegments: evidence.confirmedSegments,
        unassignedRenderedSamples: evidence.unassignedRenderedSamples,
        activeResponseId: evidence.responseId,
        ...(firstPlaybackAtMs === undefined ? {} : { firstPlaybackAtMs }),
      })
      void this.publishPlaybackConfirmation(
        evidence.responseId,
        evidence.continuousPrefix,
      ).catch(() => this.failTransport())
      if (
        firstPlaybackAtMs !== undefined
        && evidence.responseId !== ''
        && !this.playbackStartedResponses.has(evidence.responseId)
      ) {
        this.playbackStartedResponses.add(evidence.responseId)
        void this.publishPlaybackStarted(evidence.responseId, firstPlaybackAtMs).catch(() => {
          this.failTransport()
        })
      }
    })
  }

  async connect(url: string, token: string, sessionId: string): Promise<void> {
    this.recovering = true
    this.recoverySynchronized = false
    this.clearStateSync()
    this.controlProbes.reset(); this.clockProbes.reset()
    this.audioProbe?.cancel()
    if (this.sessionId !== sessionId) {
      this.coreAckOutbox?.clear()
      this.coreAckOutbox = null
      this.stoppedResponses.clear()
      this.latestResponseId = null
    }
    if (this.room === null) this.room = this.createRoom()
    const shouldSynchronize = this.reconnectRequested && this.sessionId === sessionId
    this.explicitDisconnect = false
    this.pendingDisconnectOrigin = null
    this.sessionId = sessionId
    this.startBrowserDelivery(sessionId, this.room)
    await this.room.connect(url, token)
    this.recovering = false
    if (shouldSynchronize) await this.requestStateSync(this.room)
    this.reconnectRequested = false
    this.observe({ transport: 'available', control: 'available', audio: 'unavailable' })
  }

  private packetOutputObserver: ((row: PacketOutputEvidence) => void) | undefined

  setConnectionObserver(observer: (row: ConnectionLifecycleObservation) => void): void {this.connectionObserver = observer}

  private observeConnection(event: ConnectionLifecycleObservation['event'], retry?: RetryObservation, disconnect?: DisconnectObservation): void {
    this.connectionObserver?.({event, atMs: performance.now(), generation: this.generation,
      ...(retry ? {retry} : {}), ...(disconnect ? {disconnect} : {})})
  }

  setCoreDeliveryObserver(observer: (row: CoreDeliveryObservation) => void): void {this.coreDeliveryObserver = observer}

  setDecodedReceiptObserver(observer: (row: DecodedReceiptSnapshot) => void): void {this.receiptObserver = observer}

  setStaleAudioObserver(observer: (row: StaleAudioObservation) => void): void {this.staleAudioObserver = observer}

  setPacketOutputObserver(observer: (row: PacketOutputEvidence) => void): void {
    this.packetOutputObserver = observer
  }

  probeControl(): Promise<ControlProbeObservation> {
    const room = this.room
    if (room === null || this.controlOutbox === null) return Promise.resolve({
      status: 'unavailable', generation: this.generation,
      probeId: null, sentAtMs: null, receivedAtMs: null,
    })
    return this.controlProbes.start(this.generation, async (probeId, generation) => {
      await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({
        protocol_version: '1.0', type: 'control_probe', probe_id: probeId, generation,
      })), {reliable: true, topic: PRIVATE_TOPIC})
    })
  }

  probeClock(): Promise<ControlProbeObservation> {
    const room = this.room
    if (room === null || this.controlOutbox === null || this.recovering || this.syncRequestedGeneration !== null) return Promise.resolve({
      status: 'unavailable', generation: this.generation, probeId: null, sentAtMs: null, receivedAtMs: null,
    })
    // 初回connect・状態同期の途中へ診断publishを差し込まない。
    // 復旧判定用probeとはpending状態を分け、通常の疎通指標へ時計診断を混ぜない。
    return this.clockProbes.start(this.generation, async (probeId, generation) => {
      await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({
        protocol_version: '1.0', type: 'control_probe', probe_id: probeId, generation, observe_clock: true,
      })), {reliable: true, topic: PRIVATE_TOPIC})
    })
  }

  isAudioProbeReady(): boolean {
    return this.room !== null && this.sessionId !== null && this.controlOutbox !== null
      // 制御の疎通だけではmediaの再接続完了を証明できない。
      && !this.recovering && this.syncRequestedGeneration === null
  }

  probeAudio(): Promise<AudioProbeObservation> {
    const room = this.room, sessionId = this.sessionId, generation = this.generation
    const probe = new AudioAvailabilityProbe(crypto.randomUUID(), generation,
      () => this.room === room && this.sessionId === sessionId && this.generation === generation
        && this.controlOutbox !== null,
      async frame => {
        if (room === null) throw new Error('room unavailable')
        await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(frame)),
          {reliable: true, topic: PRIVATE_TOPIC})
      })
    if (!this.isAudioProbeReady()) probe.cancel('unavailable')
    else if (this.audioProbe !== null) probe.cancel('busy')
    else {
      this.audioProbe = probe
      probe.start()
      void probe.result.then(() => {if (this.audioProbe === probe) this.audioProbe = null})
    }
    return probe.result
  }

  private isProbePublisher(participant: RemoteParticipant | undefined): participant is RemoteParticipant {
    return this.sessionId !== null && participant !== undefined && !!participant.sid
      && participant.identity.startsWith('character-')
      && participant.identity.endsWith('-' + this.sessionId)
  }

  async publishMicrophone(stream?: MediaStream): Promise<void> {
    if (this.room === null) throw new Error('LiveKit Room is not connected')
    if (stream !== undefined) {
      const track = stream.getAudioTracks()[0]
      if (track === undefined) throw new Error('Microphone stream has no audio track')
      await this.room.localParticipant.publishTrack(track, {
        source: Track.Source.Microphone,
      })
      return
    }
    await this.room.localParticipant.setMicrophoneEnabled(
      true,
      { ...this.microphoneCaptureOptions },
    )
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
    const sessionId = this.sessionId
    const outbox = this.controlOutbox
    if (sessionId === null || outbox === null) {
      throw new Error('LiveKit Room control channel is not connected')
    }
    const event = parseVoiceSessionEvent(value)
    if (event.session_id !== sessionId) {
      throw new Error('control event session_id does not match the connected session')
    }
    const payload = new TextEncoder().encode(JSON.stringify(event))
    await outbox.enqueue({ event }, payload)
  }

  stopPlayback(responseId: string, speechStartedAtMs?: number): number {
    if (this.suppressedResponseId === responseId) {
      return this.suppressedLastPlayedAudioSequence
    }
    const lastPlayedAudioSequence = Math.max(
      0,
      this.playback.continuousPrefix(responseId) + 1,
    )
    this.stoppedResponses.add(responseId)
    this.suppressedResponseId = responseId
    this.suppressedLastPlayedAudioSequence = lastPlayedAudioSequence
    this.pendingPlaybackResponseId = null
    this.playback.discardResponse(responseId)
    for (let index = this.pendingMetadata.length - 1; index >= 0; index -= 1) {
      if (this.pendingMetadata[index].responseId === responseId) {
        this.pendingMetadata.splice(index, 1)
      }
    }
    this.audioGraphResetVersion += 1
    for (const graph of this.audioGraphs.values()) {
      if (graph.responseId === responseId) this.suspendAudioGraph(graph)
    }
    const controlAvailable = this.controlOutbox !== null
    this.observe({
      transport: this.room === null ? 'idle' : controlAvailable ? 'available' : 'unavailable',
      control: controlAvailable ? 'available' : 'unavailable',
      audio: 'unavailable',
      activeAudioGraphs: 0,
      activeResponseId: responseId,
      playedPrefix: lastPlayedAudioSequence - 1,
      ...(speechStartedAtMs === undefined ? {} : { speechStartedAtMs }),
      localPlaybackStoppedAtMs: Math.floor(performance.now()),
    })
    return lastPlayedAudioSequence
  }

  disconnect(): void {
    this.controlProbes.reset(); this.clockProbes.reset()
    this.audioProbe?.cancel()
    this.explicitDisconnect = true
    this.pendingDisconnectOrigin = 'explicit'
    this.reconnectRequested = false
    this.sessionId = null
    this.room?.disconnect()
    void this.closeAudioGraph()
    this.observe({ transport: 'idle', control: 'unavailable', audio: 'unavailable' })
  }

  temporaryDisconnect(): void {
    this.controlProbes.reset(); this.clockProbes.reset()
    this.audioProbe?.cancel()
    this.reconnectRequested = true
    this.pendingDisconnectOrigin = 'temporary'
    this.room?.disconnect()
    void this.closeAudioGraph()
    this.observe({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
  }

  private createRoom(): Room {
    const room = new Room({ adaptiveStream: true, dynacast: true,
      reconnectPolicy: new VoiceReconnectPolicy(Math.random, retry => this.observeConnection('retry_scheduled', retry)) })
    room.on(RoomEvent.SignalReconnecting, () => {
      this.observeConnection('signal_reconnecting')
      this.beginStateRecovery(room)
    })
    room.on(RoomEvent.SignalConnected, () => this.observeConnection('signal_connected'))
    room.on(RoomEvent.Reconnecting, () => {
      this.observeConnection('reconnecting')
      this.beginStateRecovery(room)
      this.controlProbes.reset(); this.clockProbes.reset()
      this.audioProbe?.cancel()
      this.clearBrowserDelivery()
      this.observe({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
    })
    room.on(RoomEvent.Reconnected, () => {
      this.recovering = false
      this.observeConnection('reconnected')
      const sessionId = this.sessionId
      if (sessionId !== null) this.startBrowserDelivery(sessionId, room)
      if (!this.recoverySynchronized) void this.requestStateSync(room).catch(() => this.failTransport())
    })
    room.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
      if (topic === APPLICATION_TOPIC) {
        void this.acknowledgeCoreEvent(room, payload).catch(error => this.failTransport('transport', error))
        return
      }
      if (topic === SCREEN_TOPIC) {
        try {
          const event = parseScreenPerceptionEvent(
            JSON.parse(new TextDecoder().decode(payload)) as unknown,
          )
          if (event.type !== 'screen_snapshot_requested') throw new Error('invalid screen event')
          this.receiveScreenRequest(event)
        } catch {
          this.failTransport()
        }
        return
      }
      if (topic !== PRIVATE_TOPIC) return
      try {
        const frame = decodePrivateFrame(payload)
        if (frame.type === 'audio_probe_finished') {
          if (this.isProbePublisher(_participant)) this.audioProbe?.finish(frame.probeId, frame.generation,
            frame.trackSid, _participant.sid, frame)
          return
        }
        if (frame.type === 'control_probe_ack') {
          const clock = frame.serverReceivedAtUs === undefined ? undefined : {
            serverReceivedAtUs: frame.serverReceivedAtUs, serverSentAtUs: frame.serverSentAtUs!,
          }
          this.controlProbes.acknowledge(frame.probeId, frame.generation, clock)
          this.clockProbes.acknowledge(frame.probeId, frame.generation, clock)
          return
        }
        if (frame.type === 'authoritative_state') {
          if (frame.generation < this.generation) return
          for (const terminal of frame.terminalOutcomes) {
            this.stoppedResponses.add(terminal.responseId)
            for (const graph of this.audioGraphs.values()) {
              if (graph.responseId === terminal.responseId) this.suspendAudioGraph(graph)
            }
          }
          const generationChanged = frame.generation !== this.generation
          this.generation = frame.generation
          this.observeConnection('authoritative_state')
          if (this.syncRequestedGeneration !== null && frame.generation > this.syncRequestedGeneration
            && frame.sessionPhase === 'available') {
            this.recoverySynchronized = true
            this.clearStateSync()
          }
          if (generationChanged) {
            this.controlProbes.reset(); this.clockProbes.reset()
            this.audioProbe?.cancel()
            this.playback.setGeneration(frame.generation)
            this.pendingMetadata.length = 0
            this.pendingFinishes.clear()
            this.playbackStartedResponses.clear()
            this.firstOutputTimes.clear()
            const resetVersion = ++this.audioGraphResetVersion
            const resetTask = this.audioGraphResetTask.then(
              () => this.resetAudioGraphs(resetVersion),
            )
            this.audioGraphResetTask = resetTask.catch(() => undefined)
            void resetTask.catch(() => this.failTransport())
          }
          if (frame.sessionPhase !== 'available') {
            this.recoverySynchronized = false
            this.audioProbe?.cancel()
          }
          if (frame.sessionPhase === 'ended') {
            this.failTransport()
            return
          }
          this.observe({
            transport: frame.sessionPhase === 'available' ? 'available' : 'unavailable',
            control: 'available',
            audio: this.audioGraphs.size > 0 ? 'available' : 'unavailable',
            generation: frame.generation,
            ...(generationChanged
              ? {
                  renderedSamples: 0,
                  playedPrefix: -1,
                  activeAudioGraphs: 0,
                  renderedEnergy: 0,
                  confirmedSegments: 0,
                  unassignedRenderedSamples: 0,
                  activeResponseId: '',
                }
              : {}),
            terminalResponseId: frame.terminalOutcomes.at(-1)?.responseId ?? '',
            terminalConfirmedAudioSequence:
              frame.terminalOutcomes.at(-1)?.confirmedAudioSequence ?? 0,
          })
        } else if (frame.type === 'ack') {
          if (frame.generation !== this.generation) return
          const confirmation = this.controlOutbox?.acknowledge(frame.eventId)
          if (
            confirmation?.responseId !== undefined
            && confirmation.continuousPrefix !== undefined
          ) {
            this.observe({
              transport: 'available', control: 'available', audio: 'available',
              acknowledgedPlaybackPrefix: confirmation.continuousPrefix,
            })
          }
        } else if (frame.type === 'response_audio_finished') {
          if (frame.generation !== this.generation || this.stoppedResponses.has(frame.responseId)) return
          const graph = [...this.audioGraphs.values()].find(graph => graph.responseId === frame.responseId)
          if (graph) graph.outputTracker.finish(frame)
          else {
            if (this.pendingFinishes.size >= 128) throw new Error('pending source completion overflow')
            this.pendingFinishes.set(frame.responseId, frame)
          }
        } else if (frame.type === 'logical_audio_segment') {
          if (frame.generation !== this.generation || this.stoppedResponses.has(frame.responseId)) return
          const context = this.audioContext
          if (context === null) this.pendingMetadata.push(frame)
          else this.recordMetadataOnContext(frame, context)
        } else if (
          frame.type === 'microphone_observation'
          && frame.generation === this.generation
        ) {
          this.observe({
            transport: 'available',
            control: 'available',
            audio: 'available',
            microphoneFrames: frame.frameCount,
            microphoneSamples: frame.sampleCount,
          })
        }
      } catch {
        this.failTransport()
      }
    })
    room.on(RoomEvent.TrackPublished, (publication, participant) => {
      this.audioProbe?.observeTrack('published', publication.trackName, this.isProbePublisher(participant), publication.kind === Track.Kind.Audio)
    })
    room.on(
      RoomEvent.TrackSubscribed,
      (track: RemoteTrack, publication: RemoteTrackPublication, _participant: RemoteParticipant) => {
        this.audioProbe?.observeTrack('subscribed', publication.trackName, this.isProbePublisher(_participant), track.kind === Track.Kind.Audio)
        if (track.kind !== Track.Kind.Audio) return
        const key = publication.trackSid
        if (publication.trackName.startsWith(AUDIO_PROBE_TRACK_PREFIX)) {
          if (this.isProbePublisher(_participant) && this.audioProbe?.matchesTrack(publication.trackName)) {
            this.audioProbe.attach(track, key, _participant.sid)
          }
          return
        }
        const responseId = responseIdFromTrackName(publication.trackName)
        if (responseId === null) return
        if (this.subscriptions.has(key)) {
          this.duplicateTrackFrames += 1
          this.observe({
            transport: 'available', control: 'available', audio: 'available',
            duplicateTrackFrames: this.duplicateTrackFrames,
          })
          return
        }
        this.subscriptions.add(key)
        this.trackResponses.set(key, responseId)
        this.subscribedTracks.set(key, track)
        const receiptAudit = this.receiptObserver && this.sessionId !== null
          ? new DecodedReceiptAudit(responseId, this.sessionId, this.generation, performance.now(), this.receiptObserver) : undefined
        if (receiptAudit) this.receiptAudits.set(key, receiptAudit)
        const observer = new RemoteMediaObserver(track.receiver, track.mediaStreamTrack,
          (evidence) => this.observeTrackMedia(evidence, responseId, key), {
            packet: packet => {
              receiptAudit?.received(packet.pcm.length, performance.now())
              const graph = this.audioGraphs.get(key)
              graph?.audit?.received(packet.pcm.length)
              if (!this.subscriptions.has(key) || !graph || this.stoppedResponses.has(responseId)) return
              try {
                const gap = graph.packetSequence.receive(packet)
                if (gap) {this.interruptResponseAfterPacketLoss(responseId, gap); return}
              } catch (error) {
                const context = error instanceof RtpPacketSequenceError ? error.context : undefined
                if (context && context.packetIndex === context.expectedPacketIndex
                  && Number.isInteger(context.rtpTimestamp) && context.rtpTimestamp >= 0 && context.rtpTimestamp <= 0xffffffff
                  && Number.isInteger(context.previousRtpTimestamp) && context.previousRtpTimestamp >= 0 && context.previousRtpTimestamp <= 0xffffffff
                  && Number.isInteger(context.timestampDelta)) {
                  this.interruptResponseAfterMediaDiscontinuity(responseId, {mediaTimelineInterruption: {
                    responseId, atMs: performance.now(), reason: 'timestamp_discontinuity'}})
                } else this.failTransport('rtp_timeline', error)
                return
              }
              graph.packetDiagnostic?.receive(packet)
              if (packet.packetIndex === 0) graph.firstPacket = {...packet, pcm: new Float32Array(0)}
              graph.worklet.port.postMessage({kind: 'pcm', packetIndex: packet.packetIndex,
                rtpTimestamp: packet.rtpTimestamp, samples: packet.pcm}, [packet.pcm.buffer])
            }, failed: () => this.failTransport('media_decoder'),
            interrupted: () => {
              if (!this.subscriptions.has(key) || this.trackResponses.get(key) !== responseId) return
              this.interruptResponseAfterMediaDiscontinuity(responseId, {mediaTimelineInterruption: {
                responseId, atMs: performance.now(), reason: 'timestamp_overlap'}})
            },
          })
        this.mediaObservers.set(key, observer)
        void this.attachRenderEvidence(track, key).catch(() => this.failTransport('audio_graph'))
      },
    )
    room.on(RoomEvent.TrackUnsubscribed, (_track, publication) => {
      const key = publication.trackSid
      this.audioProbe?.unsubscribe(key)
      this.subscriptions.delete(key)
      this.subscribedTracks.delete(key)
      this.trackResponses.delete(key)
      this.trackMediaEvidence.delete(key)
      this.mediaObservers.get(key)?.close()
      this.mediaObservers.delete(key)
      this.receiptAudits.get(key)?.close(performance.now())
      this.receiptAudits.delete(key)
      const graph = this.audioGraphs.get(key)
      if (graph !== undefined) this.disconnectAudioGraph(graph)
      this.audioGraphs.delete(key)
    })
    room.on(RoomEvent.Disconnected, reason => {
      this.recovering = true
      this.recoverySynchronized = false
      this.clearStateSync()
      // SDK例外本文やtokenを含めず、数値の理由とアプリ側の切断起点だけを残す。
      const origin = this.pendingDisconnectOrigin ?? 'sdk'
      this.pendingDisconnectOrigin = null
      this.observeConnection('disconnected', undefined, {
        reason: typeof reason === 'number' && Number.isSafeInteger(reason) ? reason : null, origin,
      })
      this.controlProbes.reset(); this.clockProbes.reset()
      this.audioProbe?.cancel()
      if (!this.explicitDisconnect && this.sessionId !== null) {
        this.reconnectRequested = true
      }
      this.clearBrowserDelivery()
      void this.closeAudioGraph()
      this.observe({
        transport: this.explicitDisconnect ? 'idle' : 'unavailable',
        control: 'unavailable',
        audio: 'unavailable',
      })
      this.explicitDisconnect = false
    })
    return room
  }

  private beginStateRecovery(room: Room): void {
    this.recovering = true
    this.recoverySynchronized = false
    this.clearStateSync()
    this.controlProbes.reset(); this.clockProbes.reset()
    this.audioProbe?.cancel()
    void this.requestStateSync(room).catch(() => this.failTransport())
  }

  private clearStateSync(): void {
    this.stateSyncRequest?.close()
    this.stateSyncRequest = null
    this.syncRequestedGeneration = null
  }

  private async requestStateSync(room: Room): Promise<void> {
    if (this.stateSyncRequest !== null) return
    const generation = this.generation, sessionId = this.sessionId
    this.syncRequestedGeneration = generation
    const frame = new TextEncoder().encode(JSON.stringify({
      protocol_version: '1.0', type: 'state_sync_request', generation,
    }))
    this.stateSyncRequest = new StateSyncRequest(async () => {
      if (this.room !== room || this.sessionId !== sessionId) return
      this.observeConnection('state_sync_requested')
      await room.localParticipant.publishData(frame, {reliable: true, topic: PRIVATE_TOPIC})
    }, browserRetryTimer, () => this.failTransport('transport', 'state_sync_timeout'))
    this.stateSyncRequest.start()
  }

  private async acknowledgeCoreEvent(room: Room, payload: Uint8Array): Promise<void> {
    const { event, duplicate } = this.coreEvents.receive(payload)
    // 重複除外・Controllerの旧応答抑止より前の受信を、本文を含めず診断する。
    if (event.response_id !== undefined && (event.type === 'response_started'
      || event.type === 'response_delta' || event.type === 'response_cancelled')) {
      this.coreDeliveryObserver?.({type: event.type, sessionId: event.session_id,
        responseId: event.response_id, generation: this.generation, atMs: performance.now(), duplicate,
        textCharacters: event.text?.length ?? 0, textSequence: event.text_sequence ?? null})
    }
    if (!duplicate) {
      if (event.type === 'response_started' && event.response_id !== undefined
        && !this.stoppedResponses.has(event.response_id)) {
        if (this.latestResponseId !== null && this.latestResponseId !== event.response_id) {
          this.stoppedResponses.add(this.latestResponseId)
          this.playback.discardResponse(this.latestResponseId)
          this.firstOutputTimes.delete(this.latestResponseId)
          for (const graph of this.audioGraphs.values()) {
            if (graph.responseId === this.latestResponseId) this.suspendAudioGraph(graph)
          }
        }
        this.latestResponseId = event.response_id
      }
      if (
        event.type === 'response_started'
        && event.response_id !== undefined
        && this.suppressedResponseId !== null
        && event.response_id !== this.suppressedResponseId
      ) {
        // response_started直後は、remote trackに割り込み前の音声が残っている場合がある。
        // 次回答の最初の音声セグメントが確定するまで再生graphを復旧しない。
        this.pendingPlaybackResponseId = event.response_id
      }
      if (
        event.type === 'response_audio_segment'
        && event.response_id !== undefined
        && event.response_id === this.pendingPlaybackResponseId
      ) {
        this.suppressedResponseId = null
        this.suppressedLastPlayedAudioSequence = 0
        this.pendingPlaybackResponseId = null
        this.resumePlaybackGraphs(event.response_id)
      }
      if (
        (event.type === 'response_cancelled' || event.type === 'response_failed')
        && event.response_id !== undefined
      ) {
        this.stoppedResponses.add(event.response_id)
        this.playback.discardResponse(event.response_id)
        for (const graph of this.audioGraphs.values()) {
          if (graph.responseId === event.response_id) this.suspendAudioGraph(graph)
        }
        if (event.response_id === this.pendingPlaybackResponseId) {
          this.pendingPlaybackResponseId = null
        }
      }
      if (event.type === 'response_cancelled' && event.response_id !== undefined) {
        const confirmedAt = performance.now()
        for (const receipt of this.receiptAudits.values()) {
          if (receipt.responseId === event.response_id) receipt.cancel(confirmedAt)
        }
        for (const graph of this.audioGraphs.values()) {
          if (graph.responseId === event.response_id) graph.audit?.cancel(confirmedAt)
        }
        this.observe({
          transport: 'available',
          control: 'available',
          audio: 'unavailable',
          activeResponseId: event.response_id,
          cancelConfirmedAtMs: Math.floor(confirmedAt),
        })
      }
      this.receiveCoreEvent(event)
    }
    if (this.room === room) this.coreAckOutbox?.enqueue(event.event_id)
    if (event.type === 'session_ended') this.failTransport()
  }

  private async publishPlaybackConfirmation(
    responseId: string,
    continuousPrefix: number,
    responseFinished = false,
  ): Promise<void> {
    const tracker = this.playbackConfirmations
    const outbox = this.controlOutbox
    if (tracker === null || outbox === null) {
      throw new Error('LiveKit Room is not connected')
    }
    const confirmation = tracker.create(responseId, continuousPrefix, responseFinished)
    if (confirmation === null) return
    const payload = new TextEncoder().encode(JSON.stringify(confirmation.event))
    await outbox.enqueue(confirmation, payload)
  }

  private observeTrackMedia(evidence: MediaObservation, responseId: string, key: string): void {
    if (!this.subscriptions.has(key) || this.trackResponses.get(key) !== responseId) return
    this.trackMediaEvidence.set(key, evidence)
    // track名だけでは相関を確定せず、同じpacketのPCMが出力時計を通過するまで待つ。
    const controlAvailable = this.controlOutbox !== null
    const audioAvailable = controlAvailable && [...this.audioGraphs.values()].some((graph) => !graph.suspended)
    this.observe({
      transport: controlAvailable ? 'available' : 'unavailable',
      control: controlAvailable ? 'available' : 'unavailable',
      audio: audioAvailable ? 'available' : 'unavailable',
      mediaObservation: evidence,
      mediaTrackResponseId: responseId,
      mediaCorrelationMissingReason: 'response_frame_correlation_unavailable',
    })
  }

  private async publishPlaybackStarted(responseId: string, atMs: number): Promise<void> {
    await this.publishMediaMeasurement(responseId, 'playback_started', atMs)
  }

  private async publishMediaMeasurement(responseId: string,
    measurement: 'client_track_received' | 'client_encoded_received' | 'client_audio_decoded' | 'playback_started',
    atMs: number): Promise<void> {
    const sessionId = this.sessionId
    if (sessionId === null) throw new Error('LiveKit Room is not connected')
    await this.publishControlEvent(parseVoiceSessionEvent({
      type: 'observation',
      protocol_version: '1.0',
      event_id: crypto.randomUUID(),
      session_id: sessionId,
      response_id: responseId,
      measurement,
      timestamp: Math.floor(atMs),
      clock_domain: 'client_monotonic',
      unit: 'millisecond',
    }))
  }

  private async observeNetwork(responseId: string, key: string, generation: number, expectedPackets: number): Promise<void> {
    const room = this.room
    if (!room) return
    const sender = room.localParticipant.getTrackPublication(Track.Source.Microphone)?.track?.sender
    const receiver = this.subscribedTracks.get(key)?.receiver
    const networkObservation = await this.networkObserver.capture(sender, receiver, expectedPackets)
    if (this.room !== room || this.generation !== generation || this.trackResponses.get(key) !== responseId) return
    this.observe({transport: 'available', control: 'available', audio: 'available', networkResponseId: responseId, networkObservation})
  }

  private interruptResponseAfterPacketLoss(responseId: string, gap: RtpPacketGap): void {
    this.interruptResponseAfterMediaDiscontinuity(responseId, {mediaPacketLoss: {...gap, responseId, atMs: performance.now()}})
  }

  private interruptResponseAfterMediaDiscontinuity(responseId: string,
    evidence: Pick<RoomObservation, 'mediaPacketLoss' | 'mediaTimelineInterruption'>): void {
    const sessionId = this.sessionId, room = this.room, generation = this.generation
    if (sessionId === null || room === null || this.stoppedResponses.has(responseId)) return
    // 欠けたPCMを全出力済みに補完しない。確認済みprefixだけ通知し、現在の応答を止める。
    const lastPlayedAudioSequence = this.stopPlayback(responseId)
    this.observe({transport: 'available', control: 'available', audio: 'unavailable',
      ...evidence})
    const event = (fields: Record<string, unknown>) => parseVoiceSessionEvent({
      protocol_version: '1.0', event_id: crypto.randomUUID(), session_id: sessionId,
      response_id: responseId, reason: 'disconnect', monotonic_timestamp_ms: Math.floor(performance.now()),
      ...fields,
    })
    // reliable制御経路は維持する。Coreは停止位置を確定してから生成をcancelする。
    void (async () => {
      await this.publishControlEvent(event({type: 'playback_stopped', last_played_audio_sequence: lastPlayedAudioSequence}))
      if (this.room !== room || this.sessionId !== sessionId) return
      await this.publishControlEvent(event({type: 'response_cancel_requested'}))
      // Backendが再送期限切れでunavailableになった場合も、同じsessionの制御を戻す。
      if (this.room === room && this.sessionId === sessionId && this.generation === generation) {
        await this.requestStateSync(room)
      }
    })().catch(() => {
      if (this.room === room && this.sessionId === sessionId) this.failTransport()
    })
  }

  private failTransport(failureStage: NonNullable<RoomObservation['failureStage']> = 'transport', reason?: unknown): void {
    // 任意の例外本文を外へ渡さず、内部の固定エラー名だけを診断に残す。
    const knownReasons = ['state_sync_timeout', 'RTP packet sequence invalid', 'invalid packet render interval', 'invalid RTP timestamp', 'RTP timeline discontinuity',
      'render beyond completed source', 'output clock confirmation queue overflow', 'invalid packet output clock',
      'first output packet mismatch', 'packet_or_sample_mismatch', 'pcm_queue_overflow', 'render_clock_unreconciled']
    const message = reason instanceof Error ? reason.message : reason
    const failureReason = typeof message === 'string' && knownReasons.includes(message) ? message : 'unclassified'

    this.pendingDisconnectOrigin ??= 'transport_failure'
    this.room?.disconnect()
    void this.closeAudioGraph()
    this.observe({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable', failureStage, failureReason,
      ...(reason instanceof PacketRenderError || reason instanceof RtpPacketSequenceError ? {failureContext: reason.context} : {}) })
  }

  private async attachRenderEvidence(track: RemoteTrack, key: string): Promise<void> {
    if (!this.subscriptions.has(key)) return
    const responseId = this.trackResponses.get(key)
    if (responseId === undefined || this.stoppedResponses.has(responseId)) return
    if (this.audioContext === null) {
      // 出力待ちの音声を減らす。実際の遅延はブラウザに依存するため診断側で実測する。
      const created = new AudioContext({ sampleRate: 48_000, latencyHint: 0 })
      this.audioContext = created
      const url = URL.createObjectURL(new Blob([packetRendererSource, postGainAuditSource], { type: 'text/javascript' }))
      this.workletReady = created.audioWorklet.addModule(url)
        .finally(() => {
          URL.revokeObjectURL(url)
        })
        .catch(async (error: unknown) => {
          if (this.audioContext === created) await this.disposeAudioContext()
          throw error
        })
    }
    const context = this.audioContext
    const workletReady = this.workletReady
    const generation = this.generation
    await context.resume()
    await workletReady
    await this.mediaObservers.get(key)?.ready()
    if (!this.subscriptions.has(key) || this.audioContext !== context || this.generation !== generation) return
    const worklet = new AudioWorkletNode(context, 'packet-renderer', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
      processorOptions: {paused: this.stoppedResponses.has(responseId) || this.suppressedResponseId !== null},
    })
    const outputGain = context.createGain()
    outputGain.gain.value = 1
    const outputTracker = new PacketOutputTracker((interval, atMs, clock) => {
      const graph = this.audioGraphs.get(key)
      if (!graph || graph.suspended || this.generation !== generation || this.audioContext !== context) return
      if (interval.packetIndex === 0 && interval.packetSampleOffset === 0) {
        if (!graph.firstPacket || graph.firstPacket.rtpTimestamp !== interval.rtpTimestamp) throw new Error('first output packet mismatch')
        const media = this.trackMediaEvidence.get(key)
        const packet = graph.firstPacket
        if (!media || media.packetDecodeMissingReason !== undefined || media.firstPacketDecodedSamples !== 960
          || media.firstPacketReceivedAtMs !== packet.receivedAtMs || media.firstPacketDecodedAtMs !== packet.decodedAtMs
          || !Number.isFinite(media.trackReceivedAtMs) || media.trackReceivedAtMs < 0
          || media.trackReceivedAtMs > packet.receivedAtMs || packet.receivedAtMs > packet.decodedAtMs
          || packet.decodedAtMs > atMs) throw new Error('first output media correlation mismatch')
        this.firstOutputTimes.set(responseId, atMs)
        // 応答専用trackの最初の受信packetを復号し、そのPCMの出力通過を確認した時点で相関する。
        // 元PCMのcodec lookaheadやlogical segmentのplayed prefixとは別の観測境界。
        void Promise.all([
          this.publishMediaMeasurement(responseId, 'client_track_received', media.trackReceivedAtMs),
          this.publishMediaMeasurement(responseId, 'client_encoded_received', packet.receivedAtMs),
          this.publishMediaMeasurement(responseId, 'client_audio_decoded', packet.decodedAtMs),
        ]).catch(() => this.failTransport('media_decoder'))
        this.observe({transport: 'available', control: 'available', audio: 'available', activeResponseId: responseId,
          mediaResponseId: responseId, mediaTrackResponseId: responseId, mediaObservation: media,
          packetPlaybackObservation: {packetIndex: 0, receivedAtMs: graph.firstPacket.receivedAtMs,
            decodedAtMs: graph.firstPacket.decodedAtMs, firstOutputFrame: interval.startFrame,
            firstOutputEndFrame: interval.endFrame, firstOutputAtMs: atMs,
            outputClockContextTime: clock.contextTime, outputClockPerformanceTime: clock.performanceTime,
            confirmationObservedAtMs: clock.observedAtMs, sampleRate: 48000,
            outputClockPassed: true, sourcePcmOffsetVerified: false,
            renderClockMethod: 'quantum_count_reconciled_with_global_frame',
            renderQuantumStartFrame: interval.renderQuantumStartFrame,
            renderClockConfirmationFrame: interval.renderClockConfirmationFrame}})
      }
      graph.packetDiagnostic?.confirm(interval, atMs, clock.observedAtMs)
      this.playback.recordRenderedInterval({...interval, responseId,
        ...(interval.packetIndex === 0 && interval.packetSampleOffset === 0 ? {firstResponseFrame: interval.startFrame} : {})})
    }, completion => {
      const graph = this.audioGraphs.get(key)
      if (!graph || graph.suspended || this.generation !== generation || this.audioContext !== context) return
      clearInterval(graph.outputTimer)
      graph.worklet.port.postMessage({kind: 'stop'})
      const prefix = this.playback.metadataPrefixForTotal(responseId, completion.inputSamples)
      if (prefix < 0) throw new Error('full playback metadata does not match source samples')
      void this.publishPlaybackConfirmation(responseId, prefix, true).catch(() => this.failTransport())
      this.observe({transport: 'available', control: 'available', audio: 'available',
        activeResponseId: responseId, playbackCompletedResponseId: responseId, playbackCompletion: completion})
      void this.observeNetwork(responseId, key, generation, completion.packetCount)
    })
    const outputTimer = setInterval(() => {
      try {outputTracker.poll(context.getOutputTimestamp(), context.sampleRate)}
      catch (error) {this.failTransport('output_clock', error)}
    }, 5)
    worklet.port.onmessage = (event: MessageEvent<PacketRenderInterval | {kind: 'empty' | 'error'; reason?: string}>) => {
      if (!this.subscriptions.has(key) || this.generation !== generation || this.audioContext !== context
        || this.audioGraphs.get(key)?.suspended) return
      try {
        if (event.data.kind === 'rendered') outputTracker.record(event.data)
        else if (event.data.kind === 'error') this.failTransport('renderer', event.data.reason)
      } catch (error) {
        this.failTransport(error instanceof Error && error.message === 'RTP timeline discontinuity' ? 'rtp_timeline' : 'renderer', error)
      }
    }
    const playbackElement = document.createElement('audio')
    playbackElement.autoplay = true
    playbackElement.hidden = true
    // 要素はWebRTCのmedia処理を維持する。出音はworklet経路だけにする。
    playbackElement.muted = true
    playbackElement.srcObject = new MediaStream([track.mediaStreamTrack])
    document.body.append(playbackElement)
    const audit = this.staleAudioObserver === undefined || this.sessionId === null ? undefined
      : new PostGainAudioMonitor(context, responseId, this.sessionId, generation, this.staleAudioObserver)
    const graph = {
      responseId, audit,
      outputTracker,
      outputTimer,
      firstPacket: undefined as DecodedAudioPacket | undefined,
      packetSequence: new RtpPacketSequence(),
      packetDiagnostic: this.packetOutputObserver === undefined ? undefined
        : new PacketOutputDiagnostic(responseId, key, generation, row => this.packetOutputObserver?.(row)),
      worklet,
      outputGain,
      playbackElement,
      suspended: this.stoppedResponses.has(responseId) || this.suppressedResponseId !== null,
    }
    if (!graph.suspended) this.connectAudioEvidence(graph, context)
    this.audioGraphs.set(key, graph)
    const finished = this.pendingFinishes.get(responseId)
    if (finished) {this.pendingFinishes.delete(responseId); outputTracker.finish(finished)}
    for (const metadata of this.pendingMetadata.splice(0)) {
      this.recordMetadataOnContext(metadata, context)
    }
    if (this.room !== null && this.subscriptions.has(key) && this.audioGraphs.get(key) === graph
      && this.generation === generation && !this.stoppedResponses.has(responseId)) {
      await this.room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({
        protocol_version: '1.0', type: 'response_track_ready', response_id: responseId,
        track_sid: key, generation,
      })), {reliable: true, topic: PRIVATE_TOPIC})
    }
    if (!this.subscriptions.has(key) || this.audioContext !== context || this.generation !== generation
      || this.audioGraphs.get(key) !== graph) return
    this.observe({
      transport: 'available', control: 'available', audio: 'available',
      activeAudioGraphs: [...this.audioGraphs.values()].filter(graph => !graph.suspended).length,
    })
  }

  private async resetAudioGraphs(resetVersion: number): Promise<void> {
    const tracks = [...this.subscribedTracks.entries()].map(([key, track]) => ({
      key,
      track,
    }))
    await this.disposeAudioContext()
    if (resetVersion !== this.audioGraphResetVersion) return
    for (const { key, track } of tracks) {
      if (resetVersion !== this.audioGraphResetVersion) return
      if (this.subscriptions.has(key)) await this.attachRenderEvidence(track, key)
    }
  }

  private async closeAudioGraph(): Promise<void> {
    this.audioGraphResetVersion += 1
    for (const observer of this.mediaObservers.values()) observer.close()
    this.mediaObservers.clear()
    for (const receipt of this.receiptAudits.values()) receipt.close(performance.now())
    this.receiptAudits.clear()
    this.subscriptions.clear()
    this.subscribedTracks.clear()
    this.trackResponses.clear()
    this.trackMediaEvidence.clear()
    this.pendingMetadata.length = 0
    this.pendingFinishes.clear()
    this.firstOutputTimes.clear()
    this.suppressedResponseId = null
    this.suppressedLastPlayedAudioSequence = 0
    this.pendingPlaybackResponseId = null
    this.coreEvents.clear()
    this.recoverySynchronized = false
    this.clearStateSync()
    this.coreAckOutbox?.clear()
    this.coreAckOutbox = null
    this.clearBrowserDelivery()
    await this.disposeAudioContext()
  }

  private startBrowserDelivery(sessionId: string, room: Room): void {
    this.clearBrowserDelivery()
    this.coreAckOutbox ??= new CoreAckOutbox(async eventId => {
      if (this.room !== room || this.sessionId !== sessionId) {
        throw new Error('Core ACK transport unavailable')
      }
      await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({
        protocol_version: '1.0', type: 'ack', event_id: eventId, generation: this.generation,
      })), {reliable: true, topic: PRIVATE_TOPIC})
    }, browserRetryTimer, () => this.failTransport(), () => this.observeConnection('ack_deferred'))
    this.playbackConfirmations = new PlaybackConfirmationTracker(
      sessionId,
      () => Math.floor(performance.now()),
      () => crypto.randomUUID(),
    )
    this.controlOutbox = new BrowserControlOutbox(
      (payload) => room.localParticipant.publishData(payload, {
        reliable: true,
        topic: APPLICATION_TOPIC,
      }),
      () => this.failTransport(),
      browserRetryTimer,
    )
  }

  private clearBrowserDelivery(): void {
    this.controlOutbox?.clear()
    this.controlOutbox = null
    this.playbackConfirmations = null
  }

  private async disposeAudioContext(): Promise<void> {
    for (const graph of this.audioGraphs.values()) {
      this.disconnectAudioGraph(graph)
    }
    this.audioGraphs.clear()
    const context = this.audioContext
    this.audioContext = null
    this.workletReady = null
    await Promise.all([...this.audioAuditDisposals])
    if (context !== null) await context.close()
  }

  private disconnectAudioGraph(graph: {
    audit?: PostGainAudioMonitor
    outputTracker: PacketOutputTracker
    outputTimer: ReturnType<typeof setInterval>
    worklet: AudioWorkletNode
    outputGain: GainNode
    playbackElement: HTMLAudioElement
    suspended: boolean
  }): void {
    clearInterval(graph.outputTimer)
    graph.outputTracker.stop()
    graph.worklet.port.postMessage({kind: 'stop'})
    graph.worklet.port.close()
    if (!graph.suspended) {
      graph.worklet.disconnect()
      graph.outputGain.disconnect()
    }
    graph.playbackElement.srcObject = null
    graph.playbackElement.remove()
    if (graph.audit) {
      const task = graph.audit.dispose()
      this.audioAuditDisposals.add(task)
      void task.then(() => this.audioAuditDisposals.delete(task), () => this.audioAuditDisposals.delete(task))
    }
  }

  private suspendAudioGraph(graph: {
    outputTracker: PacketOutputTracker
    outputTimer: ReturnType<typeof setInterval>
    worklet: AudioWorkletNode
    outputGain: GainNode
    playbackElement: HTMLAudioElement
    suspended: boolean
  }): void {
    graph.playbackElement.muted = true
    clearInterval(graph.outputTimer)
    graph.outputTracker.stop()
    graph.worklet.port.postMessage({kind: 'stop'})
    if (graph.suspended) return
    graph.worklet.disconnect()
    graph.outputGain.disconnect()
    graph.suspended = true
  }

  private resumePlaybackGraphs(responseId: string): void {
    const context = this.audioContext
    if (context !== null && this.audioGraphs.size > 0) {
      for (const graph of this.audioGraphs.values()) {
        if (graph.responseId === responseId && !this.stoppedResponses.has(responseId)
          && graph.suspended) this.connectAudioEvidence(graph, context)
      }
      this.observe({
        transport: this.room === null ? 'idle' : 'available',
        control: this.controlOutbox === null ? 'unavailable' : 'available',
        audio: 'available',
        activeAudioGraphs: [...this.audioGraphs.values()].filter(graph => !graph.suspended).length,
      })
      return
    }
    const resetVersion = ++this.audioGraphResetVersion
    const resetTask = this.audioGraphResetTask.then(
      () => this.resetAudioGraphs(resetVersion),
    )
    this.audioGraphResetTask = resetTask.catch(() => undefined)
    void resetTask.catch(() => this.failTransport())
  }

  private connectAudioEvidence(
    graph: {
      audit?: PostGainAudioMonitor
      worklet: AudioWorkletNode
      outputGain: GainNode
      playbackElement: HTMLAudioElement
      suspended: boolean
    },
    context: AudioContext,
  ): void {
    graph.worklet.port.postMessage({kind: 'resume'})
    graph.worklet.connect(graph.outputGain)
    graph.outputGain.connect(graph.audit?.node ?? context.destination)
    graph.suspended = false
  }

  private recordMetadataOnContext(
    metadata: SegmentMetadata,
    context: AudioContext,
  ): void {
    this.playback.recordMetadata(
      metadata,
      Math.floor(context.currentTime * context.sampleRate),
    )
  }
}
