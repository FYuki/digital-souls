import type { VoiceSessionEvent } from '../lib/voice-session/generated'
import { parseVoiceSessionEvent } from '../lib/voice-session/validation'
import {
  endLiveKitSession,
  requestLiveKitToken,
  type TokenResponse,
} from './client'
import { LiveKitRoomClient, type RoomObservation } from './room'

export type VoiceSessionPhase =
  | 'idle'
  | 'connecting'
  | 'listening'
  | 'muted'
  | 'reconnecting'
  | 'ended'
  | 'error'

export type VoiceSessionContext = Readonly<{
  characterId: string
  conversationId: string
}>

export type VoiceSessionSnapshot = Readonly<{
  phase: VoiceSessionPhase
  context: VoiceSessionContext | null
  sessionId: string | null
}>

export type VoiceSessionRoom = {
  connect: (url: string, token: string, sessionId: string) => Promise<void>
  publishMicrophone: () => Promise<void>
  muteMicrophone: () => Promise<void>
  publishControlEvent: (event: VoiceSessionEvent) => Promise<void>
  disconnect: () => void
}

export type VoiceSessionDependencies = Readonly<{
  requestToken: (
    characterId: string,
    conversationId: string,
    sessionId?: string,
  ) => Promise<TokenResponse>
  endSession: (sessionId: string) => Promise<void>
  roomFactory: (
    observe: (observation: RoomObservation) => void,
    receiveCoreEvent: (event: VoiceSessionEvent) => void,
  ) => VoiceSessionRoom
  eventId: () => string
  monotonicMs: () => number
}>

const defaultDependencies: VoiceSessionDependencies = {
  requestToken: requestLiveKitToken,
  endSession: endLiveKitSession,
  roomFactory: (observe, receiveCoreEvent) => {
    const testPort = (globalThis as typeof globalThis & {
      __digitalSoulsVoiceSessionTestPort?: {
        createRoom?: VoiceSessionDependencies['roomFactory']
        observeRoom?: (observation: RoomObservation) => void
        receiveCoreEvent?: (event: VoiceSessionEvent) => void
      }
    }).__digitalSoulsVoiceSessionTestPort
    if (testPort?.createRoom !== undefined) {
      return testPort.createRoom(observe, receiveCoreEvent)
    }
    return new LiveKitRoomClient(
      (observation) => {
        observe(observation)
        testPort?.observeRoom?.(observation)
      },
      (event) => {
        receiveCoreEvent(event)
        testPort?.receiveCoreEvent?.(event)
      },
    )
  },
  eventId: () => crypto.randomUUID(),
  monotonicMs: () => Math.floor(performance.now()),
}

const sameContext = (
  left: VoiceSessionContext | null,
  right: VoiceSessionContext,
): boolean => left?.characterId === right.characterId
  && left.conversationId === right.conversationId

export class LiveKitVoiceSessionController {
  private phase: VoiceSessionPhase = 'idle'
  private context: VoiceSessionContext | null = null
  private binding: TokenResponse | null = null
  private room: VoiceSessionRoom | null = null
  private operationVersion = 0
  private phaseBeforeReconnect: 'listening' | 'muted' = 'muted'
  private controlTail: Promise<void> = Promise.resolve()

  constructor(
    private readonly observe: (snapshot: VoiceSessionSnapshot) => void,
    private readonly receiveCoreEvent: (event: VoiceSessionEvent) => void,
    private readonly dependencies: VoiceSessionDependencies = defaultDependencies,
  ) {
    this.publishSnapshot()
  }

  snapshot(): VoiceSessionSnapshot {
    return {
      phase: this.phase,
      context: this.context,
      sessionId: this.binding?.session_id ?? null,
    }
  }

  async ensureSession(context: VoiceSessionContext): Promise<void> {
    if (sameContext(this.context, context) && this.room !== null && this.binding !== null) {
      return
    }
    if (this.context !== null || this.room !== null || this.binding !== null) {
      await this.end()
    }
    const version = ++this.operationVersion
    this.context = context
    this.setPhase('connecting')
    try {
      const binding = await this.dependencies.requestToken(
        context.characterId,
        context.conversationId,
      )
      if (version !== this.operationVersion) return
      const room = this.dependencies.roomFactory(
        (observation) => this.receiveRoomObservation(version, observation),
        (event) => {
          if (version === this.operationVersion) this.receiveCoreEvent(event)
        },
      )
      await room.connect(binding.livekit_url, binding.token, binding.session_id)
      if (version !== this.operationVersion) {
        room.disconnect()
        return
      }
      this.binding = binding
      this.room = room
      await this.publishControlEvent(room, this.event({
        type: 'session_start_requested',
        requested_reconnect_grace_ms: binding.reconnect_grace_ms,
      }))
      if (version === this.operationVersion) this.setPhase('muted')
    } catch (error) {
      if (version === this.operationVersion) {
        this.room?.disconnect()
        this.room = null
        this.binding = null
        this.setPhase('error')
      }
      throw error
    }
  }

  async resumeMicrophone(): Promise<void> {
    const room = this.requiredRoom()
    await room.publishMicrophone()
    await this.publishControlEvent(room, this.event({ type: 'session_resumed' }))
    this.phaseBeforeReconnect = 'listening'
    this.setPhase('listening')
  }

  async muteMicrophone(): Promise<void> {
    const room = this.requiredRoom()
    await room.muteMicrophone()
    await this.publishControlEvent(room, this.event({ type: 'session_muted' }))
    this.phaseBeforeReconnect = 'muted'
    this.setPhase('muted')
  }

  async speechStarted(utteranceId: string, atMs: number): Promise<void> {
    const room = this.requiredRoom()
    await this.publishControlEvent(room, this.event({
      type: 'speech_started',
      utterance_id: utteranceId,
      speaker: this.userSpeaker(),
      monotonic_timestamp_ms: Math.floor(atMs),
    }))
  }

  async speechStopped(utteranceId: string, atMs: number): Promise<void> {
    const room = this.requiredRoom()
    await this.publishControlEvent(room, this.event({
      type: 'speech_stopped',
      utterance_id: utteranceId,
      speaker: this.userSpeaker(),
      monotonic_timestamp_ms: Math.floor(atMs),
    }))
    await this.publishControlEvent(room, this.event({
      type: 'observation',
      utterance_id: utteranceId,
      measurement: 'speech_stopped',
      timestamp: Math.floor(atMs),
      clock_domain: 'client_monotonic',
      unit: 'millisecond',
    }))
  }

  async end(): Promise<void> {
    const binding = this.binding
    const room = this.room
    ++this.operationVersion
    this.binding = null
    this.room = null
    this.controlTail = Promise.resolve()
    this.context = null
    room?.disconnect()
    this.setPhase(binding === null ? 'idle' : 'ended')
    if (binding !== null) await this.dependencies.endSession(binding.session_id)
  }

  private event(
    fields: Omit<VoiceSessionEvent, 'protocol_version' | 'event_id' | 'session_id'>,
  ): VoiceSessionEvent {
    const binding = this.binding
    if (binding === null) throw new Error('LiveKit voice session is not connected')
    const envelope: Record<string, unknown> = {
      protocol_version: '1.0',
      event_id: this.dependencies.eventId(),
      session_id: binding.session_id,
      ...fields,
    }
    if (fields.type !== 'observation' && envelope.monotonic_timestamp_ms === undefined) {
      envelope.monotonic_timestamp_ms = this.dependencies.monotonicMs()
    }
    return parseVoiceSessionEvent(envelope)
  }

  private userSpeaker() {
    const binding = this.binding
    if (binding === null) throw new Error('LiveKit voice session is not connected')
    return {
      participant_id: binding.participant_id,
      role: 'user' as const,
    }
  }

  private requiredRoom(): VoiceSessionRoom {
    if (this.room === null || this.binding === null) {
      throw new Error('LiveKit voice session is not connected')
    }
    return this.room
  }

  private publishControlEvent(
    room: VoiceSessionRoom,
    event: VoiceSessionEvent,
  ): Promise<void> {
    const operation = this.controlTail.then(() => room.publishControlEvent(event))
    this.controlTail = operation.catch(() => undefined)
    return operation
  }

  private receiveRoomObservation(version: number, observation: RoomObservation): void {
    if (version !== this.operationVersion || this.phase === 'ended') return
    if (observation.transport === 'unavailable') {
      if (this.phase === 'listening' || this.phase === 'muted') {
        this.phaseBeforeReconnect = this.phase
      }
      this.setPhase('reconnecting')
      return
    }
    if (observation.transport === 'available' && this.phase === 'reconnecting') {
      this.setPhase(this.phaseBeforeReconnect)
    }
  }

  private setPhase(phase: VoiceSessionPhase): void {
    this.phase = phase
    this.publishSnapshot()
  }

  private publishSnapshot(): void {
    this.observe(this.snapshot())
  }
}
