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

export type VoiceInputPhase = 'inactive' | 'muted' | 'listening' | 'transcribing'
export type VoiceResponsePhase = 'idle' | 'thinking' | 'generating' | 'interrupting'
export type VoicePlaybackPhase = 'idle' | 'playing' | 'stopped'

export type VoiceSessionContext = Readonly<{
  characterId: string
  conversationId: string
}>

export type VoiceSessionSnapshot = Readonly<{
  phase: VoiceSessionPhase
  input: VoiceInputPhase
  response: VoiceResponsePhase
  playback: VoicePlaybackPhase
  context: VoiceSessionContext | null
  sessionId: string | null
  activeResponseId: string | null
}>

export type VoiceSessionRoom = {
  connect: (url: string, token: string, sessionId: string) => Promise<void>
  publishMicrophone: () => Promise<void>
  muteMicrophone: () => Promise<void>
  publishControlEvent: (event: VoiceSessionEvent) => Promise<void>
  stopPlayback: (responseId: string, speechStartedAtMs: number) => number
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
        bindController?: (controller: {
          speechStarted: (utteranceId: string, atMs: number) => Promise<void>
        }) => void
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
  private input: VoiceInputPhase = 'inactive'
  private response: VoiceResponsePhase = 'idle'
  private playback: VoicePlaybackPhase = 'idle'
  private context: VoiceSessionContext | null = null
  private binding: TokenResponse | null = null
  private room: VoiceSessionRoom | null = null
  private operationVersion = 0
  private phaseBeforeReconnect: 'listening' | 'muted' = 'muted'
  private controlTail: Promise<void> = Promise.resolve()
  private generatingResponseId: string | null = null
  private playbackResponseId: string | null = null
  private playbackLastPlayedSequence = 0
  private completedPlayback: { responseId: string; lastAudioSequence: number } | null = null
  private readonly interruptedResponseIds = new Set<string>()
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null

  constructor(
    private readonly observe: (snapshot: VoiceSessionSnapshot) => void,
    private readonly receiveCoreEvent: (event: VoiceSessionEvent) => void,
    private readonly dependencies: VoiceSessionDependencies = defaultDependencies,
  ) {
    const testPort = (globalThis as typeof globalThis & {
      __digitalSoulsVoiceSessionTestPort?: {
        bindController?: (controller: {
          speechStarted: (utteranceId: string, atMs: number) => Promise<void>
        }) => void
      }
    }).__digitalSoulsVoiceSessionTestPort
    testPort?.bindController?.({
      speechStarted: (utteranceId, atMs) => this.speechStarted(utteranceId, atMs),
    })
    this.publishSnapshot()
  }

  snapshot(): VoiceSessionSnapshot {
    return {
      phase: this.phase,
      input: this.input,
      response: this.response,
      playback: this.playback,
      context: this.context,
      sessionId: this.binding?.session_id ?? null,
      activeResponseId: this.generatingResponseId ?? this.playbackResponseId,
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
          if (version === this.operationVersion) this.receiveRoomCoreEvent(event)
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
      if (version === this.operationVersion) {
        this.input = 'muted'
        this.setPhase('muted')
      }
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
    this.input = 'listening'
    this.setPhase('listening')
  }

  async muteMicrophone(): Promise<void> {
    const room = this.requiredRoom()
    await room.muteMicrophone()
    await this.publishControlEvent(room, this.event({ type: 'session_muted' }))
    this.phaseBeforeReconnect = 'muted'
    this.input = 'muted'
    this.setPhase('muted')
  }

  async speechStarted(utteranceId: string, atMs: number): Promise<void> {
    const room = this.requiredRoom()
    const playbackResponseId = this.playbackResponseId
    const generatingResponseId = this.generatingResponseId
    if (
      playbackResponseId !== null
      && !this.interruptedResponseIds.has(playbackResponseId)
    ) {
      const lastPlayedAudioSequence = room.stopPlayback(playbackResponseId, atMs)
      this.playback = 'stopped'
      this.interruptedResponseIds.add(playbackResponseId)
      void this.publishControlEvent(room, this.event({
        type: 'playback_stopped',
        response_id: playbackResponseId,
        reason: 'barge_in',
        last_played_audio_sequence: lastPlayedAudioSequence,
        monotonic_timestamp_ms: Math.floor(atMs),
      }))
    }
    if (
      generatingResponseId !== null
      && !this.interruptedResponseIds.has(`cancel:${generatingResponseId}`)
    ) {
      this.interruptedResponseIds.add(`cancel:${generatingResponseId}`)
      this.response = 'interrupting'
      void this.publishControlEvent(room, this.event({
        type: 'response_cancel_requested',
        response_id: generatingResponseId,
        reason: 'barge_in',
        monotonic_timestamp_ms: Math.floor(atMs),
      }))
    }
    this.input = 'listening'
    this.publishSnapshot()
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
    this.input = 'transcribing'
    this.publishSnapshot()
  }

  async end(): Promise<void> {
    const binding = this.binding
    const room = this.room
    ++this.operationVersion
    this.clearReconnectTimer()
    this.binding = null
    this.room = null
    this.controlTail = Promise.resolve()
    this.context = null
    this.generatingResponseId = null
    this.playbackResponseId = null
    this.playbackLastPlayedSequence = 0
    this.completedPlayback = null
    this.interruptedResponseIds.clear()
    this.input = 'inactive'
    this.response = 'idle'
    this.playback = 'idle'
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
      this.startReconnectTimer(version)
      return
    }
    if (observation.transport === 'available' && this.phase === 'reconnecting') {
      this.clearReconnectTimer()
      this.setPhase(this.phaseBeforeReconnect)
    }
    if (
      observation.activeResponseId !== undefined
      && observation.activeResponseId !== ''
    ) {
      this.playbackResponseId = observation.activeResponseId
      if (observation.playedPrefix !== undefined) {
        this.playbackLastPlayedSequence = Math.max(0, observation.playedPrefix + 1)
      }
      if (
        observation.renderedEnergy !== undefined
        && observation.renderedEnergy > 0
        && observation.audio === 'available'
      ) this.playback = 'playing'
      if (
        this.completedPlayback?.responseId === observation.activeResponseId
        && this.playbackLastPlayedSequence >= this.completedPlayback.lastAudioSequence
      ) {
        this.playback = 'idle'
        this.playbackResponseId = null
        this.completedPlayback = null
      }
      this.publishSnapshot()
    }
  }

  private receiveRoomCoreEvent(event: VoiceSessionEvent): void {
    if (event.type === 'session_ended') {
      this.terminateTransport('ended')
      this.receiveCoreEvent(event)
      return
    }
    if (event.type === 'response_started' && event.response_id !== undefined) {
      this.interruptedResponseIds.clear()
      this.generatingResponseId = event.response_id
      this.playbackResponseId = null
      this.playbackLastPlayedSequence = 0
      this.completedPlayback = null
      this.response = 'generating'
      this.playback = 'idle'
    } else if (event.type === 'response_delta' && event.response_id === this.generatingResponseId) {
      this.response = 'generating'
    } else if (event.type === 'utterance_finalized' || event.type === 'utterance_discarded') {
      this.input = this.phaseBeforeReconnect === 'muted' ? 'muted' : 'listening'
      if (
        event.type === 'utterance_finalized'
        && event.should_response === true
        && this.generatingResponseId === null
      ) {
        this.response = 'thinking'
      }
    } else if (
      ['response_completed', 'response_cancelled', 'response_failed'].includes(event.type)
      && event.response_id !== undefined
    ) {
      if (event.response_id === this.generatingResponseId) {
        this.generatingResponseId = null
        this.response = 'idle'
      }
      if (
        event.type === 'response_completed'
        && event.last_audio_sequence !== undefined
      ) {
        this.completedPlayback = {
          responseId: event.response_id,
          lastAudioSequence: event.last_audio_sequence,
        }
        if (
          event.response_id === this.playbackResponseId
          && this.playbackLastPlayedSequence >= event.last_audio_sequence
        ) {
          this.playback = 'idle'
          this.playbackResponseId = null
          this.completedPlayback = null
        }
      }
      if (event.type !== 'response_completed' && event.response_id === this.playbackResponseId) {
        this.playback = 'stopped'
      }
    } else if (event.type === 'error') {
      this.input = event.user_state === 'muted' ? 'muted' : 'listening'
      if (event.classification === 'terminal') {
        this.terminateTransport('error')
        this.receiveCoreEvent(event)
        return
      }
    }
    this.publishSnapshot()
    this.receiveCoreEvent(event)
  }

  private terminateTransport(phase: 'ended' | 'error'): void {
    ++this.operationVersion
    this.clearReconnectTimer()
    this.room?.disconnect()
    this.room = null
    this.binding = null
    this.controlTail = Promise.resolve()
    this.generatingResponseId = null
    this.playbackResponseId = null
    this.playbackLastPlayedSequence = 0
    this.completedPlayback = null
    this.interruptedResponseIds.clear()
    this.input = 'inactive'
    this.response = 'idle'
    this.playback = 'idle'
    this.setPhase(phase)
  }

  private startReconnectTimer(version: number): void {
    if (this.reconnectTimer !== null) return
    const graceMs = this.binding?.reconnect_grace_ms ?? 60_000
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      if (version !== this.operationVersion || this.phase !== 'reconnecting') return
      const binding = this.binding
      this.room?.disconnect()
      ++this.operationVersion
      this.room = null
      this.binding = null
      this.generatingResponseId = null
      this.playbackResponseId = null
      this.playbackLastPlayedSequence = 0
      this.completedPlayback = null
      this.input = 'inactive'
      this.response = 'idle'
      this.playback = 'idle'
      this.setPhase('ended')
      if (binding !== null) void this.dependencies.endSession(binding.session_id)
    }, graceMs)
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer === null) return
    clearTimeout(this.reconnectTimer)
    this.reconnectTimer = null
  }

  private setPhase(phase: VoiceSessionPhase): void {
    this.phase = phase
    this.publishSnapshot()
  }

  private publishSnapshot(): void {
    this.observe(this.snapshot())
  }
}
