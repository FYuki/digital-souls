import { CONVERSATION_ID_FIELD } from '../conversation-contract'
import type { ConversationTurn } from '../conversations/types'
import { parsePersistedTurn } from '../conversations/turn-parser'

const TEXT_MESSAGE_TYPE = 'text'
const ERROR_MESSAGE_TYPE = 'error'
const AUDIO_RESPONSE_METADATA_MESSAGE_TYPE = 'audio_response_metadata'

type BackendPersistedTurnMessage = {
  type: typeof TEXT_MESSAGE_TYPE
  turn: ConversationTurn
}

export type BackendErrorMessage = {
  status: number
  detail: string
}

export type AudioRequestMetadata = {
  eventId: string
  sessionId: string
  utteranceId: string
  capturedAudioStartClientMs?: number
  vadSpeechEndClientMs?: number
  utteranceFinalizedClientMs?: number
  responseDecisionClientMs?: number
  requiredManualOperations?: number
}

export type AudioResponseMetadata = {
  sessionId: string
  utteranceId: string
  responseId: string
}

export type ClientMeasurementEvent = AudioResponseMetadata & {
  eventId: string
  name: 'client_audio_received' | 'first_playback'
  timestamp: number
}

type BackendErrorEnvelope = BackendErrorMessage & {
  type: typeof ERROR_MESSAGE_TYPE
}

export type TransportCallbacks = {
  onTurnMessage: (turn: ConversationTurn) => void
  onAudioMessage: (audio: ArrayBuffer) => void
  onAudioResponseMetadata?: (metadata: AudioResponseMetadata) => void
  onError: (error: BackendErrorMessage) => void
  onTransportError: (error: Error) => void
  onOpen: () => void
  onClose: () => void
}

export interface AudioTransport {
  readonly connected: boolean
  connect: () => Promise<void>
  disconnect: () => void
  sendAudio: (pcmData: ArrayBuffer, metadata?: AudioRequestMetadata) => void
  sendMeasurementEvent: (event: ClientMeasurementEvent) => void
}

const isRecord = (value: unknown): value is Record<string, unknown> => {
  return typeof value === 'object' && value !== null
}

const isBackendErrorEnvelope = (value: unknown): value is BackendErrorEnvelope => {
  return (
    isRecord(value) &&
    value.type === ERROR_MESSAGE_TYPE &&
    typeof value.status === 'number' &&
    typeof value.detail === 'string'
  )
}

const persistedTurnMessage = (value: unknown): BackendPersistedTurnMessage | null => {
  if (!isRecord(value) || value.type !== TEXT_MESSAGE_TYPE || !('turn' in value)) return null
  return { type: TEXT_MESSAGE_TYPE, turn: parsePersistedTurn(value.turn) }
}

const audioResponseMetadata = (value: unknown): AudioResponseMetadata | null => {
  if (
    !isRecord(value)
    || value.type !== AUDIO_RESPONSE_METADATA_MESSAGE_TYPE
    || typeof value.session_id !== 'string'
    || typeof value.utterance_id !== 'string'
    || typeof value.response_id !== 'string'
  ) return null

  return {
    sessionId: value.session_id,
    utteranceId: value.utterance_id,
    responseId: value.response_id,
  }
}

export class WebSocketAudioTransport implements AudioTransport {
  #socket: WebSocket | null = null
  #connected = false

  constructor(
    private readonly webSocketUrl: string,
    private readonly conversationId: string,
    private readonly callbacks: TransportCallbacks,
  ) {}

  get connected(): boolean {
    return this.#connected
  }

  connect(): Promise<void> {
    const url = new URL(this.webSocketUrl)
    url.searchParams.set(CONVERSATION_ID_FIELD, this.conversationId)
    const socket = new WebSocket(url.toString())
    this.#socket = socket
    socket.binaryType = 'arraybuffer'

    return new Promise((resolve, reject) => {
      socket.onopen = () => {
        this.#connected = true
        this.callbacks.onOpen()
        resolve()
      }

      socket.onclose = () => {
        this.#connected = false
        this.callbacks.onClose()
      }

      socket.onerror = () => {
        const error = new Error('WebSocket connection failed')

        if (this.#connected) {
          this.callbacks.onTransportError(error)
          return
        }

        reject(error)
      }

      socket.onmessage = (event: MessageEvent<string | ArrayBuffer | Blob>) => {
        this.handleMessage(event.data)
      }
    })
  }

  disconnect(): void {
    if (this.#socket === null) {
      return
    }

    this.#socket.close()
    this.#socket = null
    this.#connected = false
  }

  sendAudio(pcmData: ArrayBuffer, metadata?: AudioRequestMetadata): void {
    const socket = this.getOpenSocket()
    if (metadata !== undefined) {
      socket.send(JSON.stringify({
        type: 'audio_metadata',
        event_id: metadata.eventId,
        session_id: metadata.sessionId,
        utterance_id: metadata.utteranceId,
        ...(metadata.capturedAudioStartClientMs === undefined ? {} : {
          captured_audio_start_client_ms: metadata.capturedAudioStartClientMs,
          vad_speech_end_client_ms: metadata.vadSpeechEndClientMs,
          utterance_finalized_client_ms: metadata.utteranceFinalizedClientMs,
          response_decision_client_ms: metadata.responseDecisionClientMs,
          required_manual_operations: metadata.requiredManualOperations,
        }),
      }))
    }
    socket.send(pcmData)
  }

  sendMeasurementEvent(event: ClientMeasurementEvent): void {
    this.getOpenSocket().send(JSON.stringify({
      type: 'measurement_event',
      event_id: event.eventId,
      session_id: event.sessionId,
      utterance_id: event.utteranceId,
      response_id: event.responseId,
      name: event.name,
      timestamp: event.timestamp,
      clock_domain: 'client_monotonic',
      unit: 'millisecond',
    }))
  }

  private getOpenSocket(): WebSocket {
    if (this.#socket === null || !this.#connected) {
      throw new Error('WebSocket is not connected')
    }

    return this.#socket
  }

  private handleMessage(data: string | ArrayBuffer | Blob): void {
    if (typeof data === 'string') {
      this.handleTextFrame(data)
      return
    }

    if (data instanceof Blob) {
      void new Response(data).arrayBuffer().then((audio) => {
        this.callbacks.onAudioMessage(audio)
      })
      return
    }

    this.callbacks.onAudioMessage(data)
  }

  private handleTextFrame(data: string): void {
    const parsed = parseBackendMessage(data)

    const persisted = persistedTurnMessage(parsed)
    if (persisted !== null) {
      this.callbacks.onTurnMessage(persisted.turn)
      return
    }

    const responseMetadata = audioResponseMetadata(parsed)
    if (responseMetadata !== null) {
      this.callbacks.onAudioResponseMetadata?.(responseMetadata)
      return
    }

    if (isBackendErrorEnvelope(parsed)) {
      this.callbacks.onError({ status: parsed.status, detail: parsed.detail })
      return
    }

    throw new Error('WebSocket message shape is invalid')
  }
}

const parseBackendMessage = (data: string): unknown => {
  try {
    return JSON.parse(data)
  } catch {
    throw new Error('WebSocket message shape is invalid')
  }
}
