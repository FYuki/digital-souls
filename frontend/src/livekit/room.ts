import {
  Room,
  RoomEvent,
  Track,
  type RemoteTrack,
  type RemoteTrackPublication,
  type RemoteParticipant,
} from 'livekit-client'

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
}>

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
      if (energy > 0) {
        this.port.postMessage({
          startFrame: currentFrame,
          endFrame: currentFrame + samples,
          energy,
        })
      }
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
  private readonly subscriptions = new Set<string>()
  private readonly subscribedTracks = new Map<string, RemoteTrack>()
  private readonly pendingMetadata: SegmentMetadata[] = []
  private readonly audioGraphs = new Map<string, {
    source: MediaStreamAudioSourceNode
    worklet: AudioWorkletNode
  }>()
  private duplicateTrackFrames = 0
  private readonly playback: PlaybackEvidenceController
  private readonly coreEvents = new CoreEventReceiver()
  private playbackConfirmations: PlaybackConfirmationTracker | null = null
  private controlOutbox: BrowserControlOutbox | null = null
  private sessionId: string | null = null
  private explicitDisconnect = false

  constructor(private readonly observe: (observation: RoomObservation) => void) {
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
    })
  }

  async connect(url: string, token: string, sessionId: string): Promise<void> {
    if (this.room === null) this.room = this.createRoom()
    this.explicitDisconnect = false
    this.sessionId = sessionId
    this.startBrowserDelivery(sessionId, this.room)
    await this.room.connect(url, token)
    this.observe({ transport: 'available', control: 'available', audio: 'unavailable' })
  }

  async publishMicrophone(): Promise<void> {
    if (this.room === null) throw new Error('LiveKit Room is not connected')
    await this.room.localParticipant.setMicrophoneEnabled(true)
  }

  disconnect(): void {
    this.explicitDisconnect = true
    this.sessionId = null
    this.room?.disconnect()
    void this.closeAudioGraph()
    this.observe({ transport: 'idle', control: 'unavailable', audio: 'unavailable' })
  }

  temporaryDisconnect(): void {
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
      const frame = new TextEncoder().encode(JSON.stringify({
        protocol_version: '1.0',
        type: 'state_sync_request',
        generation: this.generation,
      }))
      void room.localParticipant.publishData(frame, {
        reliable: true,
        topic: PRIVATE_TOPIC,
      })
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
      const frame = decodePrivateFrame(payload)
      if (frame.type === 'authoritative_state') {
        const generationChanged = frame.generation !== this.generation
        this.generation = frame.generation
        this.playback.setGeneration(frame.generation)
        if (generationChanged) {
          this.pendingMetadata.length = 0
          void this.resetAudioGraphs()
        }
        if (frame.sessionPhase === 'ended') {
          this.failTransport()
          return
        }
        this.observe({
          transport: 'unavailable', control: 'available', audio: 'unavailable', generation: frame.generation,
          renderedSamples: 0, playedPrefix: -1, activeAudioGraphs: 0,
          renderedEnergy: 0, confirmedSegments: 0, unassignedRenderedSamples: 0,
          terminalResponseId: frame.terminalOutcomes.at(-1)?.responseId,
          terminalConfirmedAudioSequence: frame.terminalOutcomes.at(-1)?.confirmedAudioSequence,
        })
      } else if (frame.type === 'ack') {
        if (frame.generation !== this.generation) return
        const confirmation = this.controlOutbox?.acknowledge(frame.eventId)
        if (confirmation !== null && confirmation !== undefined) {
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
        void this.attachRenderEvidence(track, key)
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

  private async acknowledgeCoreEvent(room: Room, payload: Uint8Array): Promise<void> {
    const { event } = this.coreEvents.receive(payload)
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

  private failTransport(): void {
    this.room?.disconnect()
    void this.closeAudioGraph()
    this.observe({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
  }

  private async attachRenderEvidence(track: RemoteTrack, key: string): Promise<void> {
    if (!this.subscriptions.has(key)) return
    if (this.audioContext === null) {
      this.audioContext = new AudioContext()
      const url = URL.createObjectURL(new Blob([workletSource], { type: 'text/javascript' }))
      this.workletReady = this.audioContext.audioWorklet.addModule(url).finally(() => {
        URL.revokeObjectURL(url)
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

  private async resetAudioGraphs(): Promise<void> {
    const tracks = [...this.subscribedTracks.entries()].map(([key, track]) => ({
      key,
      track,
    }))
    await this.disposeAudioContext()
    for (const { key, track } of tracks) {
      if (this.subscriptions.has(key)) await this.attachRenderEvidence(track, key)
    }
  }

  private async closeAudioGraph(): Promise<void> {
    this.subscriptions.clear()
    this.subscribedTracks.clear()
    this.pendingMetadata.length = 0
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
