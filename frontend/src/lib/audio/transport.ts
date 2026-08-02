import { CONVERSATION_ID_FIELD } from '../conversation-contract'
import type { ConversationTurn } from '../conversations/types'
import { parsePersistedTurn } from '../conversations/turn-parser'

const TEXT_MESSAGE_TYPE = 'text'
const ERROR_MESSAGE_TYPE = 'error'

type BackendPersistedTurnMessage = {
  type: typeof TEXT_MESSAGE_TYPE
  turn: ConversationTurn
}

export type BackendErrorMessage = {
  status: number
  detail: string
}

type BackendErrorEnvelope = BackendErrorMessage & {
  type: typeof ERROR_MESSAGE_TYPE
}

export type TransportCallbacks = {
  onTurnMessage: (turn: ConversationTurn) => void
  onAudioMessage: (audio: ArrayBuffer) => void
  onError: (error: BackendErrorMessage) => void
  onTransportError: (error: Error) => void
  onOpen: () => void
  onClose: () => void
}

export interface AudioTransport {
  readonly connected: boolean
  connect: () => Promise<void>
  disconnect: () => void
  sendAudio: (pcmData: ArrayBuffer) => void
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

  sendAudio(pcmData: ArrayBuffer): void {
    this.getOpenSocket().send(pcmData)
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
