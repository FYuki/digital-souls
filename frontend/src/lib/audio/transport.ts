const TEXT_MESSAGE_TYPE = 'text'
const ERROR_MESSAGE_TYPE = 'error'
const USER_SPEAKER = 'user'
const MIORI_SPEAKER = 'miori'

export type TextMessageSpeaker = typeof USER_SPEAKER | typeof MIORI_SPEAKER

type BackendTextMessage = {
  type: typeof TEXT_MESSAGE_TYPE
  response: string
}

type BackendUserTextMessage = {
  type: typeof TEXT_MESSAGE_TYPE
  speaker: typeof USER_SPEAKER
  message: string
}

type BackendMioriTextMessage = {
  type: typeof TEXT_MESSAGE_TYPE
  speaker: typeof MIORI_SPEAKER
  response: string
}

export type BackendErrorMessage = {
  status: number
  detail: string
}

type BackendErrorEnvelope = BackendErrorMessage & {
  type: typeof ERROR_MESSAGE_TYPE
}

export type TransportCallbacks = {
  onTextMessage: (speaker: TextMessageSpeaker, text: string) => void
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
  sendText: (message: string) => void
  sendAudio: (pcmData: ArrayBuffer) => void
}

const isRecord = (value: unknown): value is Record<string, unknown> => {
  return typeof value === 'object' && value !== null
}

const isBackendTextMessage = (value: unknown): value is BackendTextMessage => {
  return (
    isRecord(value) &&
    value.type === TEXT_MESSAGE_TYPE &&
    value.speaker === undefined &&
    typeof value.response === 'string'
  )
}

const isBackendUserTextMessage = (value: unknown): value is BackendUserTextMessage => {
  return (
    isRecord(value) &&
    value.type === TEXT_MESSAGE_TYPE &&
    value.speaker === USER_SPEAKER &&
    typeof value.message === 'string'
  )
}

const isBackendMioriTextMessage = (value: unknown): value is BackendMioriTextMessage => {
  return (
    isRecord(value) &&
    value.type === TEXT_MESSAGE_TYPE &&
    value.speaker === MIORI_SPEAKER &&
    typeof value.response === 'string'
  )
}

const isBackendErrorEnvelope = (value: unknown): value is BackendErrorEnvelope => {
  return (
    isRecord(value) &&
    value.type === ERROR_MESSAGE_TYPE &&
    typeof value.status === 'number' &&
    typeof value.detail === 'string'
  )
}

export class WebSocketAudioTransport implements AudioTransport {
  #socket: WebSocket | null = null
  #connected = false

  constructor(
    private readonly webSocketUrl: string,
    private readonly callbacks: TransportCallbacks,
  ) {}

  get connected(): boolean {
    return this.#connected
  }

  connect(): Promise<void> {
    const socket = new WebSocket(this.webSocketUrl)
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

  sendText(message: string): void {
    this.getOpenSocket().send(JSON.stringify({ type: TEXT_MESSAGE_TYPE, message }))
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

    if (isBackendUserTextMessage(parsed)) {
      this.callbacks.onTextMessage(parsed.speaker, parsed.message)
      return
    }

    if (isBackendMioriTextMessage(parsed)) {
      this.callbacks.onTextMessage(parsed.speaker, parsed.response)
      return
    }

    if (isBackendTextMessage(parsed)) {
      this.callbacks.onTextMessage(MIORI_SPEAKER, parsed.response)
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
