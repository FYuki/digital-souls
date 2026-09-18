import type {DecodedReceiptSnapshot} from './decoded-receipt-audit'
import type {ControlProbeObservation} from './control-probe'
import type {CoreDeliveryObservation} from './core-delivery-observation'
import type {StaleAudioObservation} from './post-gain-monitor'
import type { VoiceSessionEvent } from '../lib/voice-session/generated'
import { parseVoiceSessionEvent } from '../lib/voice-session/validation'
import {
  endLiveKitSession,
  requestLiveKitToken,
  VoicePreparationError,
  type TokenResponse,
} from './client'
import { LiveKitRoomClient, type RoomObservation } from './room'
import type { SnapshotRequested } from '../lib/screen-perception/generated'
import {TextSubmissionTracker, type TextSubmission} from './text-input'
import {InputSuppressionPolicy} from './input-suppression'

export type VoiceSessionPhase =
  | 'idle'
  | 'connecting'
  | 'listening'
  | 'muted'
  | 'reconnecting'
  | 'ended'
  | 'error'

export type VoiceInputPhase = 'inactive' | 'muted' | 'suppressed' | 'listening' | 'transcribing'
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
  textSubmissions: readonly TextSubmission[]
  preparationError?: string
}>

export type VoiceSessionRoom = {
  connect: (url: string, token: string, sessionId: string) => Promise<void>
  publishMicrophone: (stream: MediaStream) => Promise<string>
  muteMicrophone: () => Promise<void>
  publishControlEvent: (event: VoiceSessionEvent) => Promise<void>
  stopPlayback: (responseId: string, speechStartedAtMs?: number) => number
  disconnect: () => void
}

export type VoiceSessionDependencies = Readonly<{
  requestToken: (
    characterId: string,
    conversationId: string,
    sessionId?: string,
    screenClientSessionId?: string | null,
    signal?: AbortSignal,
  ) => Promise<TokenResponse>
  endSession: (sessionId: string) => Promise<void>
  roomFactory: (
    observe: (observation: RoomObservation) => void,
    receiveCoreEvent: (event: VoiceSessionEvent) => void,
    receiveScreenRequest: (event: SnapshotRequested) => void,
  ) => VoiceSessionRoom
  eventId: () => string
  monotonicMs: () => number
}>

const defaultDependencies: VoiceSessionDependencies = {
  requestToken: requestLiveKitToken,
  endSession: endLiveKitSession,
  roomFactory: (observe, receiveCoreEvent, receiveScreenRequest) => {
    const testPort = (globalThis as typeof globalThis & {
      __digitalSoulsVoiceSessionTestPort?: {
        createRoom?: VoiceSessionDependencies['roomFactory']
        observeRoom?: (observation: RoomObservation) => void
        observeCoreDelivery?: (observation: CoreDeliveryObservation) => void
        observeDecodedReceipts?: (row: DecodedReceiptSnapshot) => void
        observeStaleAudio?: (observation: StaleAudioObservation) => void
        bindClockProbe?: (probe: () => Promise<ControlProbeObservation>) => void
        bindRoom?: (room: Pick<LiveKitRoomClient, 'probeControl' | 'setPacketOutputObserver'>) => void
        receiveCoreEvent?: (event: VoiceSessionEvent) => void
      }
    }).__digitalSoulsVoiceSessionTestPort
    if (testPort?.createRoom !== undefined) {
      return testPort.createRoom(observe, receiveCoreEvent, receiveScreenRequest)
    }
    const room = new LiveKitRoomClient(
      (observation) => {
        observe(observation)
        testPort?.observeRoom?.(observation)
      },
      (event) => {
        receiveCoreEvent(event)
        testPort?.receiveCoreEvent?.(event)
      },
      undefined,
      receiveScreenRequest,
    )
    if (testPort?.observeCoreDelivery) room.setCoreDeliveryObserver(testPort.observeCoreDelivery)
    if (testPort?.observeDecodedReceipts) room.setDecodedReceiptObserver(testPort.observeDecodedReceipts)
    if (testPort?.observeStaleAudio) room.setStaleAudioObserver(testPort.observeStaleAudio)
    testPort?.bindClockProbe?.(() => room.probeClock())
    testPort?.bindRoom?.(room)
    return room
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
  private preparationAbort: AbortController | null = null
  private preparationError: string | null = null
  private input: VoiceInputPhase = 'inactive'
  private response: VoiceResponsePhase = 'idle'
  private playback: VoicePlaybackPhase = 'idle'
  private context: VoiceSessionContext | null = null
  private binding: TokenResponse | null = null
  private room: VoiceSessionRoom | null = null
  private operationVersion = 0
  private sessionSummary = {
    sequence: 0, microphone_activation_attempts: 0, mute_attempts: 0,
    retry_attempts: 0, operation_tracking_started: false, end_requested: false,
  }
  private pendingRetryAttempts = 0
  private ending: Promise<void> | null = null
  private microphoneEnabled = false
  private inputSuppression = new InputSuppressionPolicy()
  private microphoneStream: MediaStream | null = null
  private microphoneTail: Promise<void> = Promise.resolve()
  private focusRevision = 0
  private inputGatePending = false
  private audioRevision = 0
  private inputGeneration: number | null = null
  private authorizedTrackSid: string | null = null
  private pendingAudioOpen: {
    eventId: string; trackSid: string; revision: number
    resolve: (event: VoiceSessionEvent) => void; reject: (error: Error) => void
    timer: ReturnType<typeof setTimeout>
  } | null = null
  private controlTail: Promise<void> = Promise.resolve()
  private generatingResponseId: string | null = null
  private playbackResponseId: string | null = null
  private playbackLastPlayedSequence = 0
  private readonly renderCompletedResponses = new Set<string>()
  private completedPlayback: { responseId: string; lastAudioSequence: number } | null = null
  private readonly interruptedResponseIds = new Set<string>()
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private screenClientSessionId: string | null = null
  private receiveScreenRequest: (event: SnapshotRequested) => void = () => undefined
  private readonly textInputs = new TextSubmissionTracker()
  private textResultTimer: ReturnType<typeof setTimeout> | null = null

  constructor(
    private readonly observe: (snapshot: VoiceSessionSnapshot) => void,
    private readonly receiveCoreEvent: (event: VoiceSessionEvent, context: VoiceSessionContext) => void,
    private readonly dependencies: VoiceSessionDependencies = defaultDependencies,
  ) {
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
      textSubmissions: this.textInputs.snapshot(),
      ...(this.preparationError === null ? {} : {preparationError: this.preparationError}),
    }
  }

  matchesContext(context: VoiceSessionContext): boolean {
    return sameContext(this.context, context) && this.binding !== null && this.room !== null
  }

  canSubmitText(context: VoiceSessionContext): boolean {
    return this.matchesContext(context) && (this.phase === 'listening' || this.phase === 'muted')
      && !this.textInputs.hasPending(context)
  }

  async interruptResponse(context: VoiceSessionContext): Promise<void> {
    if (!this.matchesContext(context)) throw new Error('response belongs to another thread')
    const responseId = this.generatingResponseId ?? this.playbackResponseId
    if (responseId === null) return
    const room = this.requiredRoom()
    const lastPlayed = room.stopPlayback(responseId)
    this.interruptedResponseIds.add(responseId)
    this.playback = 'stopped'
    if (this.generatingResponseId === responseId) this.response = 'interrupting'
    this.publishSnapshot()
    await Promise.all([
      room.publishControlEvent(this.event({type: 'playback_stopped',
        response_id: responseId, reason: 'barge_in', last_played_audio_sequence: lastPlayed})),
      room.publishControlEvent(this.event({type: 'response_cancel_requested',
        response_id: responseId, reason: 'barge_in'})),
    ])
  }

  async submitText(context: VoiceSessionContext, text: string): Promise<string> {
    if (!this.canSubmitText(context)) throw new Error('text input is unavailable for this thread')
    const room = this.requiredRoom()
    this.invalidateInputGate()
    const event = this.event({type: 'user_text_submitted', text, speaker: this.userSpeaker(), input_revision: ++this.audioRevision})
    this.textInputs.begin(event, context)
    this.publishSnapshot()
    this.scheduleTextResultQuery()
    // local stopは同期部分で即時実行する。cancelの通信待ちでtext受付を遅らせない。
    // BEも新しいtext受付で旧応答をcancelし、取消の成立を決定する。
    void this.interruptResponse(context).catch(() => undefined)
    try {
      await room.publishControlEvent(event)
      if (event.input_revision === this.audioRevision && room === this.room) {
        void this.queueMicrophoneOperation(() => this.openInputGate(room)).catch(() => this.setPhase('error'))
      }
    } catch {
      // publish失敗はBE未受理の証拠ではない。本文と元のIDを保持する。
      this.textInputs.markUnknown(event.session_id, event.event_id)
      this.publishSnapshot()
    }
    return event.event_id
  }

  private scheduleTextResultQuery(): void {
    if (this.textResultTimer !== null || this.binding === null || this.phase === 'reconnecting'
      || this.textInputs.pending(this.binding.session_id).length === 0) return
    this.textResultTimer = setTimeout(() => {
      this.textResultTimer = null
      if (this.binding !== null) this.textInputs.markUnknown(this.binding.session_id)
      this.publishSnapshot()
      void this.reconcileTextInputs()
    }, 5_000)
  }

  private async reconcileTextInputs(): Promise<void> {
    const room = this.room, binding = this.binding
    if (room === null || binding === null || this.phase === 'reconnecting') return
    try {
      for (const entry of this.textInputs.pending(binding.session_id)) {
        const event = this.event({type: 'user_input_result_requested',
          input_event_id: entry.inputId, speaker: this.userSpeaker()})
        // 元のpublish完了待ちで照合要求を塞がない。再送はroomのoutboxが所有する。
        await room.publishControlEvent(event)
      }
    } catch {
      this.textInputs.markUnknown(binding.session_id)
      this.publishSnapshot()
    } finally {
      this.scheduleTextResultQuery()
    }
  }

  private clearTextResultTimer(): void {
    if (this.textResultTimer !== null) clearTimeout(this.textResultTimer)
    this.textResultTimer = null
  }

  private preserveUnconfirmedText(): void {
    this.clearTextResultTimer()
    if (this.binding !== null) this.textInputs.markUnknown(this.binding.session_id)
  }

  setScreenIntegration(
    clientSessionId: string | null,
    receiveRequest: (event: SnapshotRequested) => void,
  ): void {
    this.screenClientSessionId = clientSessionId
    this.receiveScreenRequest = receiveRequest
  }

  async ensureSession(context: VoiceSessionContext): Promise<void> {
    if (this.ending !== null) await this.ending
    if (sameContext(this.context, context) && this.room !== null && this.binding !== null) {
      return
    }
    if (this.context !== null || this.room !== null || this.binding !== null) {
      await this.end()
    }
    const version = ++this.operationVersion
    this.inputSuppression = new InputSuppressionPolicy()
    this.inputGatePending = false
    this.audioRevision = 0
    this.inputGeneration = null
    this.authorizedTrackSid = null
    this.microphoneStream = null
    this.context = context
    this.preparationError = null
    const preparationAbort = this.preparationAbort = new AbortController()
    this.setPhase('connecting')
    // 接続確定前の資源はこの開始処理が所有し、終了・別Session開始後も回収する。
    let openingBinding: TokenResponse | null = null
    let openingRoom: VoiceSessionRoom | null = null
    let handedOff = false
    try {
      const binding = openingBinding = await this.dependencies.requestToken(
        context.characterId,
        context.conversationId,
        undefined,
        this.screenClientSessionId,
        preparationAbort.signal,
      )
      if (version !== this.operationVersion) throw new Error('音声Sessionの開始は取り消されました')
      const room = openingRoom = this.dependencies.roomFactory(
        (observation) => this.receiveRoomObservation(version, observation),
        (event) => {
          if (version === this.operationVersion) this.receiveRoomCoreEvent(event)
        },
        (event) => {
          if (version === this.operationVersion) this.receiveScreenRequest(event)
        },
      )
      await room.connect(binding.livekit_url, binding.token, binding.session_id)
      if (version !== this.operationVersion) throw new Error('音声Sessionの開始は取り消されました')
      this.binding = binding
      this.textInputs.releaseResolvedSessions(binding.session_id)
      this.room = room
      // ここからはend()／transport終端処理がSessionを所有する。
      handedOff = true
      this.sessionSummary = {
        sequence: 0, microphone_activation_attempts: 0, mute_attempts: 0,
        retry_attempts: this.pendingRetryAttempts, operation_tracking_started: this.pendingRetryAttempts > 0, end_requested: false,
      }
      this.pendingRetryAttempts = 0
      await this.publishControlEvent(room, this.event({
        type: 'session_start_requested',
        requested_reconnect_grace_ms: binding.reconnect_grace_ms,
      }))
      // 取消を成功扱いすると呼び出し元がgetUserMediaへ進んでしまう。
      if (version !== this.operationVersion) throw new Error('音声Sessionの開始は取り消されました')
      this.microphoneEnabled = false
      this.input = 'muted'
      this.setPhase('muted')
    } catch (error) {
      if (version === this.operationVersion) {
        // 接続失敗後の遅延observerでerrorから再接続中へ戻さない。
        ++this.operationVersion
        this.clearReconnectTimer()
        this.invalidateInputGate()
        handedOff = false
        this.room = null
        this.binding = null
        this.controlTail = Promise.resolve()
        this.microphoneEnabled = false
        this.input = 'inactive'
        this.preparationError = error instanceof VoicePreparationError ? error.message
          : '音声会話の準備に失敗しました。接続を確認して再試行してください。'
        this.setPhase('error')
      }
      throw error
    } finally {
      if (this.preparationAbort === preparationAbort) this.preparationAbort = null
      if (!handedOff) {
        try {
          openingRoom?.disconnect()
        } finally {
          // 終了API失敗で元の開始失敗を置換せず、未処理rejectionも残さない。
          if (openingBinding !== null) {
            await this.dependencies.endSession(openingBinding.session_id).catch(() => undefined)
          }
        }
      }
    }
  }

  recordMicrophoneActivationAttempt(): void {
    this.requiredRoom()
    this.sessionSummary.operation_tracking_started = true
    this.sessionSummary.microphone_activation_attempts += 1
    void this.publishSessionSummary().catch(() => undefined)
  }

  recordRetryAttempt(): void {
    if (this.binding === null || this.room === null) {
      this.pendingRetryAttempts += 1
      return
    }
    this.sessionSummary.operation_tracking_started = true
    this.sessionSummary.retry_attempts += 1
    void this.publishSessionSummary().catch(() => undefined)
  }

  private publishSessionSummary(endRequested = false): Promise<void> {
    const room = this.requiredRoom()
    this.sessionSummary.sequence += 1
    this.sessionSummary.end_requested = endRequested
    return this.publishControlEvent(room, this.event({
      type: 'observation', measurement: 'session_summary',
      timestamp: Math.floor(this.dependencies.monotonicMs()),
      clock_domain: 'client_monotonic', unit: 'millisecond',
      session_summary: { ...this.sessionSummary },
    }))
  }

  private invalidateInputGate(): void {
    ++this.audioRevision
    this.inputGeneration = null
    this.authorizedTrackSid = null
    this.inputGatePending = true
    this.setMicrophoneTracks(false)
    const pending = this.pendingAudioOpen
    this.pendingAudioOpen = null
    if (pending !== null) {
      clearTimeout(pending.timer)
      pending.reject(new Error('音声入力の状態が変更されました'))
    }
  }

  private async openInputGate(room: VoiceSessionRoom): Promise<void> {
    const stream = this.microphoneStream
    if (stream === null || room !== this.room || !this.microphoneEnabled
      || this.inputSuppression.suppressed || this.phase === 'reconnecting') return
    this.invalidateInputGate()
    const openingRevision = this.audioRevision
    let trackSid: string
    try {
      await room.muteMicrophone()
      trackSid = await room.publishMicrophone(stream)
    } catch (error) {
      if (room !== this.room || openingRevision !== this.audioRevision) return
      this.inputSuppression.mute()
      this.microphoneEnabled = false
      this.refreshMicrophoneState()
      throw error
    }
    if (room !== this.room || openingRevision !== this.audioRevision || this.inputSuppression.suppressed) return
    const event = this.event({type: 'audio_input_open_requested', track_sid: trackSid,
      input_revision: ++this.audioRevision, speaker: this.userSpeaker()})
    const revision = this.audioRevision
    const opened = new Promise<VoiceSessionEvent>((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pendingAudioOpen?.eventId !== event.event_id) return
        this.pendingAudioOpen = null
        reject(new Error('マイク入力の開始を確認できませんでした。もう一度マイクをオンにしてください'))
      }, 5_000)
      this.pendingAudioOpen = {eventId: event.event_id, trackSid, revision, resolve, reject, timer}
    })
    try {
      const [, result] = await Promise.all([this.publishControlEvent(room, event), opened])
      if (room !== this.room || revision !== this.audioRevision || this.inputSuppression.suppressed) return
      this.inputGeneration = result.input_generation!
      this.authorizedTrackSid = trackSid
      this.inputGatePending = false
      this.refreshMicrophoneState()
    } catch (error) {
      if (this.pendingAudioOpen?.eventId === event.event_id) {
        clearTimeout(this.pendingAudioOpen.timer)
        this.pendingAudioOpen = null
      }
      if (room !== this.room || revision !== this.audioRevision) return
      this.inputSuppression.mute()
      this.microphoneEnabled = false
      this.refreshMicrophoneState()
      throw error
    }
  }

  async resumeMicrophone(stream: MediaStream): Promise<void> {
    const room = this.requiredRoom()
    this.inputSuppression.resumeExplicitly()
    this.microphoneStream = stream
    this.invalidateInputGate()
    this.microphoneEnabled = !this.hasPersistentMute()
    const event = this.event({type: 'session_resumed', input_revision: ++this.audioRevision})
    return this.queueMicrophoneOperation(async () => {
      await this.publishControlEvent(room, event)
      if (room !== this.room) return
      await this.openInputGate(room)
      this.refreshMicrophoneState()
    })
  }

  async muteMicrophone(): Promise<void> {
    this.inputSuppression.mute()
    this.sessionSummary.mute_attempts += 1
    void this.publishSessionSummary().catch(() => undefined)
    return this.applyPersistentMute()
  }

  async muteForThreadSwitch(): Promise<void> {
    this.inputSuppression.switchThread()
    await this.applyPersistentMute()
    await this.publishFocusSuppression()
  }

  async setTextInputFocused(context: VoiceSessionContext, focused: boolean): Promise<void> {
    if (!this.matchesContext(context)) return
    if (this.inputSuppression.snapshot().includes('text_focus') === focused) return
    this.inputSuppression.setFocused(focused)
    this.invalidateInputGate()
    // focusではdeviceを即時無音化し、回答生成・再生には触れない。
    if (focused) this.setMicrophoneTracks(false)
    this.refreshMicrophoneState(false)
    await this.publishFocusSuppression()
  }

  private hasPersistentMute(): boolean {
    return this.inputSuppression.snapshot().some(reason => reason !== 'text_focus')
  }

  private setMicrophoneTracks(enabled: boolean): void {
    for (const track of this.microphoneStream?.getAudioTracks() ?? []) track.enabled = enabled
  }

  private refreshMicrophoneState(updateTracks = true): void {
    const enabled = this.microphoneEnabled && !this.inputSuppression.suppressed && !this.inputGatePending
    if (updateTracks) this.setMicrophoneTracks(enabled && this.phase !== 'reconnecting')
    this.input = !this.microphoneEnabled || this.hasPersistentMute() ? 'muted'
      : enabled ? 'listening' : 'suppressed'
    if (this.phase !== 'reconnecting') this.phase = this.microphoneEnabled ? 'listening' : 'muted'
    this.publishSnapshot()
  }

  private queueMicrophoneOperation(operation: () => Promise<void>): Promise<void> {
    const room = this.room
    const next = this.microphoneTail.then(async () => {
      if (room !== this.room) return
      await operation()
    })
    this.microphoneTail = next.catch(() => undefined)
    return next
  }

  private async applyPersistentMute(): Promise<void> {
    const room = this.requiredRoom()
    this.invalidateInputGate()
    this.microphoneEnabled = false
    this.refreshMicrophoneState()
    const event = this.event({type: 'session_muted', input_revision: ++this.audioRevision})
    return this.queueMicrophoneOperation(async () => {
      await room.muteMicrophone()
      if (room !== this.room) return
      await this.publishControlEvent(room, event)
    })
  }

  private async publishFocusSuppression(): Promise<void> {
    const revision = ++this.focusRevision
    if (this.phase === 'reconnecting') return
    const room = this.requiredRoom()
    const event = this.event({type: 'audio_input_suppression_changed', reason: 'text_focus',
      suppressed: this.inputSuppression.snapshot().includes('text_focus'),
      input_revision: ++this.audioRevision})
    return this.queueMicrophoneOperation(async () => {
      await this.publishControlEvent(room, event)
      if (room === this.room && revision === this.focusRevision) {
        await this.openInputGate(room)
        this.refreshMicrophoneState()
      }
    })
  }

  end(): Promise<void> {
    if (this.ending !== null) return this.ending
    const operation = this.finishEnd()
    this.ending = operation
    void operation.finally(() => {
      if (this.ending === operation) this.ending = null
    }).catch(() => undefined)
    return operation
  }

  private async finishEnd(): Promise<void> {
    this.preparationAbort?.abort()
    this.preparationAbort = null
    this.preparationError = null
    const binding = this.binding
    const room = this.room
    this.preserveUnconfirmedText()
    ++this.operationVersion
    this.clearReconnectTimer()
    if (binding !== null && room !== null) {
      let timer: ReturnType<typeof setTimeout> | undefined
      try {
        // ack待ちは最大500ms。欠落はBackend側で欠測となり、終了を妨げない。
        await Promise.race([
          this.publishSessionSummary(true).catch(() => undefined),
          new Promise<void>(resolve => { timer = setTimeout(resolve, 500) }),
        ])
      } finally {
        if (timer !== undefined) clearTimeout(timer)
      }
    }
    this.invalidateInputGate()
    this.binding = null
    this.room = null
    this.controlTail = Promise.resolve()
    this.context = null
    this.generatingResponseId = null
    this.playbackResponseId = null
    this.playbackLastPlayedSequence = 0
    this.completedPlayback = null
    this.interruptedResponseIds.clear()
    this.interruptingUtterances.clear()
    this.microphoneEnabled = false
    this.microphoneStream = null
    this.inputSuppression = new InputSuppressionPolicy()
    this.microphoneTail = Promise.resolve()
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
      protocol_version: '2.0',
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
    if (observation.recoveryStopped) {
      const binding = this.binding
      this.setMicrophoneTracks(false)
      this.terminateTransport('error')
      if (binding !== null) void this.dependencies.endSession(binding.session_id).catch(() => undefined)
      return
    }
    if (observation.transport === 'unavailable') {
      this.preserveUnconfirmedText()
      this.invalidateInputGate()
      this.setPhase('reconnecting')
      this.startReconnectTimer(version)
      return
    }
    if (observation.transport === 'available' && this.phase === 'reconnecting') {
      this.clearReconnectTimer()
      this.setPhase(this.microphoneEnabled ? 'listening' : 'muted')
      void this.publishFocusSuppression().catch(() => undefined)
      void this.reconcileTextInputs()
    }
    if (observation.playbackCompletedResponseId) this.renderCompletedResponses.add(observation.playbackCompletedResponseId)
    if (
      observation.activeResponseId !== undefined
      && observation.activeResponseId !== ''
      && !this.interruptedResponseIds.has(observation.activeResponseId)
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
        && this.renderCompletedResponses.has(observation.activeResponseId)
      ) {
        this.playback = 'idle'
        this.playbackResponseId = null
        this.completedPlayback = null
      }
      this.publishSnapshot()
    }
  }

  private receiveRoomCoreEvent(event: VoiceSessionEvent): void {
    if (event.session_id !== this.binding?.session_id) return
    if (event.type === 'audio_input_opened' || event.type === 'audio_input_rejected') {
      const pending = this.pendingAudioOpen
      if (pending === null || event.request_event_id !== pending.eventId) return
      if (event.type === 'audio_input_opened' && (event.track_sid !== pending.trackSid
        || event.input_revision !== pending.revision || !Number.isSafeInteger(event.input_generation)
        || event.input_generation! < 1)) return
      clearTimeout(pending.timer)
      this.pendingAudioOpen = null
      if (event.type === 'audio_input_rejected') pending.reject(new Error('音声入力を開始できませんでした: ' + event.reason))
      else {
        // 送信完了を待つ間の停止通知にも対応できるよう、ACKの世代を先に保持する。
        // マイク有効化はopenInputGateの完了・revision確認後に限定する。
        this.inputGeneration = event.input_generation!
        this.authorizedTrackSid = event.track_sid!
        pending.resolve(event)
      }
      return
    }
    if (event.type === 'speech_started' || event.type === 'speech_stopped') {
      if (event.input_generation !== this.inputGeneration || event.track_sid !== this.authorizedTrackSid
        || this.inputSuppression.suppressed || this.inputGatePending) return
      this.input = event.type === 'speech_started' ? 'listening' : 'transcribing'
      this.publishSnapshot()
      this.notifyCoreEvent(event)
      return
    }
    if (event.response_id !== undefined && this.interruptedResponseIds.has(event.response_id)
      && (event.type === 'response_delta' || event.type === 'response_started')) return
    if (event.type === 'user_input_result') {
      if (event.session_id !== this.binding?.session_id) return
      if (!this.textInputs.receive(event)) return
      if (this.textInputs.pending(event.session_id).length === 0) this.clearTextResultTimer()
      this.publishSnapshot()
      this.notifyCoreEvent(event)
      return
    }
    if (event.type === 'response_privacy_skipped' && event.response_id !== undefined) {
      this.interruptedResponseIds.add(event.response_id)
    }
    if (event.type === 'response_started') this.renderCompletedResponses.clear()
    if (event.type === 'turn_decision' && (event.final || event.decision === 'take_turn')) {
      this.publishInterruptionObservation('turn_decision_received', event.utterance_id, event.response_id)
    }
    if (event.type === 'response_cancelled' && event.response_id !== undefined) {
      this.interruptedResponseIds.add(event.response_id)
      this.publishInterruptionObservation(
        'cancel_confirmed', this.interruptingUtterances.get(event.response_id), event.response_id,
      )
      this.interruptingUtterances.delete(event.response_id)
    }
    if (event.type === 'session_ended') {
      this.terminateTransport('ended')
      this.notifyCoreEvent(event)
      return
    }
    if (
      event.type === 'turn_decision'
      && event.decision === 'take_turn'
      && event.response_id !== undefined
    ) {
      this.stopInterruptedPlayback(event.response_id, event.utterance_id)
      if (event.response_id === this.generatingResponseId) {
        this.response = 'interrupting'
      }
    } else if (event.type === 'response_started' && event.response_id !== undefined) {
      this.generatingResponseId = event.response_id
      this.playbackResponseId = null
      this.playbackLastPlayedSequence = 0
      this.completedPlayback = null
      this.response = 'generating'
      this.playback = 'idle'
    } else if (event.type === 'response_delta' && event.response_id === this.generatingResponseId) {
      this.response = 'generating'
    } else if (event.type === 'utterance_finalized' || event.type === 'utterance_discarded') {
      this.refreshMicrophoneState()
      if (
        event.type === 'utterance_finalized'
        && event.should_response === true
        && this.generatingResponseId === null
      ) {
        this.response = 'thinking'
      }
    } else if (
      ['response_completed', 'response_cancelled', 'response_failed', 'response_privacy_skipped'].includes(event.type)
      && event.response_id !== undefined
    ) {
      if (event.response_id === this.generatingResponseId
        || (event.type === 'response_privacy_skipped' && this.generatingResponseId === null)) {
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
          && (event.last_audio_sequence === 0 || this.renderCompletedResponses.has(event.response_id))
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
      if (event.error_code === 'audio_input_unavailable') {
        if (event.input_revision !== this.audioRevision
          || event.input_generation !== this.inputGeneration
          || event.track_sid !== this.authorizedTrackSid) return
        this.invalidateInputGate()
      }
      if (event.user_state === 'muted') {
        this.inputSuppression.mute()
        this.microphoneEnabled = false
      }
      this.refreshMicrophoneState()
      if (event.classification === 'terminal') {
        this.terminateTransport('error')
        this.notifyCoreEvent(event)
        return
      }
    }
    this.publishSnapshot()
    this.notifyCoreEvent(event)
  }

  private notifyCoreEvent(event: VoiceSessionEvent): void {
    if (this.context !== null) this.receiveCoreEvent(event, this.context)
  }

  private publishInterruptionObservation(
    measurement: 'turn_decision_received' | 'cancel_confirmed' | 'local_playback_stopped',
    utteranceId?: string,
    responseId?: string,
    atMs = this.dependencies.monotonicMs(),
  ): void {
    if (this.room === null || utteranceId === undefined || responseId === undefined) return
    void this.publishControlEvent(this.room, this.event({
      type: 'observation', measurement, utterance_id: utteranceId, response_id: responseId,
      timestamp: Math.floor(atMs),
      clock_domain: 'client_monotonic', unit: 'millisecond',
    }))
  }

  private readonly interruptingUtterances = new Map<string, string>()

  private stopInterruptedPlayback(responseId: string, utteranceId?: string): void {
    const room = this.room
    if (room === null || this.interruptedResponseIds.has(responseId)) return
    const lastPlayedAudioSequence = room.stopPlayback(responseId)
    const atMs = this.dependencies.monotonicMs()
    if (utteranceId !== undefined) this.interruptingUtterances.set(responseId, utteranceId)
    this.playback = 'stopped'
    this.interruptedResponseIds.add(responseId)
    void this.publishControlEvent(room, this.event({
      type: 'playback_stopped',
      response_id: responseId,
      reason: 'barge_in',
      last_played_audio_sequence: lastPlayedAudioSequence,
      monotonic_timestamp_ms: Math.floor(atMs),
    }))
    this.publishInterruptionObservation('local_playback_stopped', utteranceId, responseId, atMs)
  }

  private terminateTransport(phase: 'ended' | 'error'): void {
    this.preserveUnconfirmedText()
    this.invalidateInputGate()
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
    this.interruptingUtterances.clear()
    this.microphoneEnabled = false
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
      this.preserveUnconfirmedText()
      this.room?.disconnect()
      ++this.operationVersion
      this.room = null
      this.binding = null
      this.generatingResponseId = null
      this.playbackResponseId = null
      this.playbackLastPlayedSequence = 0
      this.completedPlayback = null
      this.microphoneEnabled = false
      this.input = 'inactive'
      this.response = 'idle'
      this.playback = 'idle'
      this.setPhase('ended')
      if (binding !== null) {
        void this.dependencies.endSession(binding.session_id).catch(() => undefined)
      }
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
