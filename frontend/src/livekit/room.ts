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

export type RoomObservation = Readonly<{
  transport: 'available' | 'unavailable' | 'idle'
  control: 'available' | 'unavailable'
  audio: 'available' | 'unavailable'
  generation?: number
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
  cancelConfirmedAtMs?: number
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

const PRIVATE_TOPIC = 'digital-souls.livekit-transport.v1'
const APPLICATION_TOPIC = 'digital-souls.core.v1'
const browserRetryTimer: RetryTimer = {
  now: () => performance.now(),
  schedule: (callback, delayMs) => setTimeout(callback, delayMs),
  cancel: (handle) => clearTimeout(handle),
}

const workletSource = `
class RenderEvidenceProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0]
    const channel = input && input[0]
    const samples = channel ? channel.length : 0
    if (samples > 0) {
      let energy = 0
      for (const sample of channel) energy += sample * sample
      this.port.postMessage({
        startFrame: currentFrame,
        endFrame: currentFrame + samples,
        energy,
      })
    }
    return true
  }
}
registerProcessor('render-evidence-processor', RenderEvidenceProcessor)
`

export class LiveKitRoomClient {
  private room: Room | null = null
  private audioContext: AudioContext | null = null
  private workletReady: Promise<void> | null = null
  private generation = 0
  private audioGraphResetVersion = 0
  private audioGraphResetTask: Promise<void> = Promise.resolve()
  private readonly subscriptions = new Set<string>()
  private readonly subscribedTracks = new Map<string, RemoteTrack>()
  private readonly pendingMetadata: SegmentMetadata[] = []
  private readonly audioGraphs = new Map<string, {
    source: MediaStreamAudioSourceNode
    worklet: AudioWorkletNode
  }>()
  private duplicateTrackFrames = 0
  private readonly playbackStartedResponses = new Set<string>()
  private readonly playback: PlaybackEvidenceController
  private readonly coreEvents = new CoreEventReceiver()
  private playbackConfirmations: PlaybackConfirmationTracker | null = null
  private controlOutbox: BrowserControlOutbox | null = null
  private sessionId: string | null = null
  private explicitDisconnect = false
  private reconnectRequested = false
  private suppressedResponseId: string | null = null
  private suppressedLastPlayedAudioSequence = 0

  constructor(
    private readonly observe: (observation: RoomObservation) => void,
    private readonly receiveCoreEvent: (event: VoiceSessionEvent) => void = () => undefined,
    private readonly microphoneCaptureOptions: MicrophoneCaptureOptions = DEFAULT_MICROPHONE_CAPTURE_OPTIONS,
  ) {
    this.playback = new PlaybackEvidenceController(0, (evidence) => {
      this.observe({
        transport: 'available',
        control: 'available',
        audio: 'available',
        renderedSamples: evidence.renderedSamples,
        playedPrefix: evidence.continuousPrefix,
        duplicateTrackFrames: this.duplicateTrackFrames,
        activeAudioGraphs: this.audioGraphs.size,
        renderedEnergy: evidence.renderedEnergy,
        confirmedSegments: evidence.confirmedSegments,
        unassignedRenderedSamples: evidence.unassignedRenderedSamples,
        activeResponseId: evidence.responseId,
      })
      void this.publishPlaybackConfirmation(
        evidence.responseId,
        evidence.continuousPrefix,
      ).catch(() => this.failTransport())
      if (
        evidence.renderedEnergy > 0
        && evidence.responseId !== ''
        && !this.playbackStartedResponses.has(evidence.responseId)
      ) {
        this.playbackStartedResponses.add(evidence.responseId)
        void this.publishPlaybackStarted(evidence.responseId).catch(() => {
          this.failTransport()
        })
      }
    })
  }

  async connect(url: string, token: string, sessionId: string): Promise<void> {
    if (this.room === null) this.room = this.createRoom()
    const shouldSynchronize = this.reconnectRequested && this.sessionId === sessionId
    this.explicitDisconnect = false
    this.sessionId = sessionId
    this.startBrowserDelivery(sessionId, this.room)
    await this.room.connect(url, token)
    if (shouldSynchronize) await this.requestStateSync(this.room)
    this.reconnectRequested = false
    this.observe({ transport: 'available', control: 'available', audio: 'unavailable' })
  }

  async publishMicrophone(): Promise<void> {
    if (this.room === null) throw new Error('LiveKit Room is not connected')
    await this.room.localParticipant.setMicrophoneEnabled(
      true,
      { ...this.microphoneCaptureOptions },
    )
  }

  async muteMicrophone(): Promise<void> {
    if (this.room === null) throw new Error('LiveKit Room is not connected')
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

  stopPlayback(responseId: string, speechStartedAtMs: number): number {
    if (this.suppressedResponseId === responseId) {
      return this.suppressedLastPlayedAudioSequence
    }
    const lastPlayedAudioSequence = Math.max(
      0,
      this.playback.continuousPrefix(responseId) + 1,
    )
    this.suppressedResponseId = responseId
    this.suppressedLastPlayedAudioSequence = lastPlayedAudioSequence
    this.playback.discardResponse(responseId)
    for (let index = this.pendingMetadata.length - 1; index >= 0; index -= 1) {
      if (this.pendingMetadata[index].responseId === responseId) {
        this.pendingMetadata.splice(index, 1)
      }
    }
    this.audioGraphResetVersion += 1
    for (const graph of this.audioGraphs.values()) {
      graph.source.disconnect()
      graph.worklet.disconnect()
    }
    this.audioGraphs.clear()
    const context = this.audioContext
    this.audioContext = null
    this.workletReady = null
    if (context !== null) void context.close().catch(() => undefined)
    const controlAvailable = this.controlOutbox !== null
    this.observe({
      transport: this.room === null ? 'idle' : controlAvailable ? 'available' : 'unavailable',
      control: controlAvailable ? 'available' : 'unavailable',
      audio: 'unavailable',
      activeAudioGraphs: 0,
      activeResponseId: responseId,
      playedPrefix: lastPlayedAudioSequence - 1,
      speechStartedAtMs,
      localPlaybackStoppedAtMs: Math.floor(performance.now()),
    })
    return lastPlayedAudioSequence
  }

  disconnect(): void {
    this.explicitDisconnect = true
    this.reconnectRequested = false
    this.sessionId = null
    this.room?.disconnect()
    void this.closeAudioGraph()
    this.observe({ transport: 'idle', control: 'unavailable', audio: 'unavailable' })
  }

  temporaryDisconnect(): void {
    this.reconnectRequested = true
    this.room?.disconnect()
    void this.closeAudioGraph()
    this.observe({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
  }

  private createRoom(): Room {
    const room = new Room({ adaptiveStream: true, dynacast: true })
    room.on(RoomEvent.Reconnecting, () => {
      this.clearBrowserDelivery()
      this.observe({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
    })
    room.on(RoomEvent.Reconnected, () => {
      const sessionId = this.sessionId
      if (sessionId !== null) this.startBrowserDelivery(sessionId, room)
      void this.requestStateSync(room).catch(() => this.failTransport())
    })
    room.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
      if (topic === APPLICATION_TOPIC) {
        void this.acknowledgeCoreEvent(room, payload).catch(() => {
          room.disconnect()
          void this.closeAudioGraph()
          this.observe({
            transport: 'unavailable', control: 'unavailable', audio: 'unavailable',
          })
        })
        return
      }
      if (topic !== PRIVATE_TOPIC) return
      try {
        const frame = decodePrivateFrame(payload)
        if (frame.type === 'authoritative_state') {
          const generationChanged = frame.generation !== this.generation
          this.generation = frame.generation
          if (generationChanged) {
            this.playback.setGeneration(frame.generation)
            this.pendingMetadata.length = 0
            this.playbackStartedResponses.clear()
            const resetVersion = ++this.audioGraphResetVersion
            const resetTask = this.audioGraphResetTask.then(
              () => this.resetAudioGraphs(resetVersion),
            )
            this.audioGraphResetTask = resetTask.catch(() => undefined)
            void resetTask.catch(() => this.failTransport())
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
        } else if (frame.type === 'logical_audio_segment') {
          if (frame.generation !== this.generation) return
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
    room.on(
      RoomEvent.TrackSubscribed,
      (track: RemoteTrack, publication: RemoteTrackPublication, _participant: RemoteParticipant) => {
        if (track.kind !== Track.Kind.Audio) return
        const key = publication.trackSid
        if (this.subscriptions.has(key)) {
          this.duplicateTrackFrames += 1
          this.observe({
            transport: 'available', control: 'available', audio: 'available',
            duplicateTrackFrames: this.duplicateTrackFrames,
          })
          return
        }
        this.subscriptions.add(key)
        this.subscribedTracks.set(key, track)
        void this.attachRenderEvidence(track, key).catch(() => this.failTransport())
      },
    )
    room.on(RoomEvent.TrackUnsubscribed, (_track, publication) => {
      const key = publication.trackSid
      this.subscriptions.delete(key)
      this.subscribedTracks.delete(key)
      const graph = this.audioGraphs.get(key)
      graph?.source.disconnect()
      graph?.worklet.disconnect()
      this.audioGraphs.delete(key)
    })
    room.on(RoomEvent.Disconnected, () => {
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

  private async requestStateSync(room: Room): Promise<void> {
    const frame = new TextEncoder().encode(JSON.stringify({
      protocol_version: '1.0',
      type: 'state_sync_request',
      generation: this.generation,
    }))
    await room.localParticipant.publishData(frame, {
      reliable: true,
      topic: PRIVATE_TOPIC,
    })
  }

  private async acknowledgeCoreEvent(room: Room, payload: Uint8Array): Promise<void> {
    const { event, duplicate } = this.coreEvents.receive(payload)
    const ack = new TextEncoder().encode(JSON.stringify({
      protocol_version: '1.0',
      type: 'ack',
      event_id: event.event_id,
      generation: this.generation,
    }))
    await room.localParticipant.publishData(ack, {
      reliable: true,
      topic: PRIVATE_TOPIC,
    })
    if (!duplicate) {
      if (
        event.type === 'response_started'
        && event.response_id !== undefined
        && this.suppressedResponseId !== null
        && event.response_id !== this.suppressedResponseId
      ) {
        this.suppressedResponseId = null
        this.suppressedLastPlayedAudioSequence = 0
        const resetVersion = ++this.audioGraphResetVersion
        const resetTask = this.audioGraphResetTask.then(
          () => this.resetAudioGraphs(resetVersion),
        )
        this.audioGraphResetTask = resetTask.catch(() => undefined)
        void resetTask.catch(() => this.failTransport())
      }
      if (
        (event.type === 'response_cancelled' || event.type === 'response_failed')
        && event.response_id !== undefined
      ) {
        this.playback.discardResponse(event.response_id)
      }
      if (event.type === 'response_cancelled' && event.response_id !== undefined) {
        this.observe({
          transport: 'available',
          control: 'available',
          audio: 'unavailable',
          activeResponseId: event.response_id,
          cancelConfirmedAtMs: Math.floor(performance.now()),
        })
      }
      this.receiveCoreEvent(event)
    }
    if (event.type === 'session_ended') this.failTransport()
  }

  private async publishPlaybackConfirmation(
    responseId: string,
    continuousPrefix: number,
  ): Promise<void> {
    const tracker = this.playbackConfirmations
    const outbox = this.controlOutbox
    if (tracker === null || outbox === null) {
      throw new Error('LiveKit Room is not connected')
    }
    const confirmation = tracker.create(responseId, continuousPrefix)
    if (confirmation === null) return
    const payload = new TextEncoder().encode(JSON.stringify(confirmation.event))
    await outbox.enqueue(confirmation, payload)
  }

  private async publishPlaybackStarted(responseId: string): Promise<void> {
    const sessionId = this.sessionId
    if (sessionId === null) throw new Error('LiveKit Room is not connected')
    await this.publishControlEvent(parseVoiceSessionEvent({
      type: 'observation',
      protocol_version: '1.0',
      event_id: crypto.randomUUID(),
      session_id: sessionId,
      response_id: responseId,
      measurement: 'playback_started',
      timestamp: Math.floor(performance.now()),
      clock_domain: 'client_monotonic',
      unit: 'millisecond',
    }))
  }

  private failTransport(): void {
    this.room?.disconnect()
    void this.closeAudioGraph()
    this.observe({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
  }

  private async attachRenderEvidence(track: RemoteTrack, key: string): Promise<void> {
    if (!this.subscriptions.has(key)) return
    if (this.audioContext === null) {
      const created = new AudioContext()
      this.audioContext = created
      const url = URL.createObjectURL(new Blob([workletSource], { type: 'text/javascript' }))
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
    if (!this.subscriptions.has(key) || this.audioContext !== context) return
    const source = context.createMediaStreamSource(
      new MediaStream([track.mediaStreamTrack]),
    )
    const worklet = new AudioWorkletNode(context, 'render-evidence-processor')
    worklet.port.onmessage = (event: MessageEvent<{
      startFrame: number
      endFrame: number
      energy: number
    }>) => {
      if (this.subscriptions.has(key) && this.generation === generation) {
        this.playback.recordRenderedInterval(event.data)
      }
    }
    source.connect(worklet).connect(context.destination)
    this.audioGraphs.set(key, { source, worklet })
    for (const metadata of this.pendingMetadata.splice(0)) {
      this.recordMetadataOnContext(metadata, context)
    }
    this.observe({
      transport: 'available', control: 'available', audio: 'available',
      activeAudioGraphs: this.audioGraphs.size,
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
    this.subscriptions.clear()
    this.subscribedTracks.clear()
    this.pendingMetadata.length = 0
    this.suppressedResponseId = null
    this.suppressedLastPlayedAudioSequence = 0
    this.coreEvents.clear()
    this.clearBrowserDelivery()
    await this.disposeAudioContext()
  }

  private startBrowserDelivery(sessionId: string, room: Room): void {
    this.clearBrowserDelivery()
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
      graph.source.disconnect()
      graph.worklet.disconnect()
    }
    this.audioGraphs.clear()
    const context = this.audioContext
    this.audioContext = null
    this.workletReady = null
    if (context !== null) await context.close()
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
