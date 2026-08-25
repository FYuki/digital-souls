import type { Page } from '@playwright/test'

type MockWebSocketFrame =
  | { kind: 'text'; data: string }
  | { kind: 'binary'; data: number[] }
  | { kind: 'audio-response-metadata'; responseId: string }

type MockWebSocketBackend = {
  textFrames: MockWebSocketFrame[]
  binaryFrames: MockWebSocketFrame[]
}

export const installMockWebSocketBackend = async (page: Page, backend: MockWebSocketBackend) => {
  await page.addInitScript((mockBackend) => {
    let observedUrls: string[] = []
    Object.defineProperty(window, '__mockWebSocketUrls', { get: () => observedUrls })
    type AudioCorrelation = { sessionId: string; utteranceId: string }
    const createFrameData = (
      frame: MockWebSocketFrame,
      correlation: AudioCorrelation | null,
    ): string | ArrayBuffer => {
      if (frame.kind === 'text') {
        return frame.data
      }
      if (frame.kind === 'audio-response-metadata') {
        if (correlation === null) {
          throw new Error('audio response metadata requires request correlation')
        }
        return JSON.stringify({
          type: 'audio_response_metadata',
          session_id: correlation.sessionId,
          utterance_id: correlation.utteranceId,
          response_id: frame.responseId,
        })
      }

      return new Uint8Array(frame.data).buffer
    }

    class MockWebSocket extends EventTarget {
      static readonly CONNECTING = 0
      static readonly OPEN = 1
      static readonly CLOSING = 2
      static readonly CLOSED = 3

      readonly url: string
      readonly protocol = ''
      readonly extensions = ''
      binaryType: BinaryType = 'blob'
      bufferedAmount = 0
      readyState = MockWebSocket.CONNECTING
      onopen: ((event: Event) => void) | null = null
      onmessage: ((event: MessageEvent) => void) | null = null
      onerror: ((event: Event) => void) | null = null
      onclose: ((event: CloseEvent) => void) | null = null
      private audioCorrelation: AudioCorrelation | null = null

      constructor(url: string | URL) {
        super()
        this.url = String(url)
        observedUrls = [...observedUrls, this.url]
        window.setTimeout(() => {
          this.readyState = MockWebSocket.OPEN
          const event = new Event('open')
          this.onopen?.(event)
          this.dispatchEvent(event)
        }, 0)
      }

      send(data: string | ArrayBufferLike | Blob | ArrayBufferView) {
        if (typeof data === 'string') {
          const parsed = JSON.parse(data) as {
            type?: unknown
            session_id?: unknown
            utterance_id?: unknown
          }
          if (
            parsed.type === 'audio_metadata'
            && typeof parsed.session_id === 'string'
            && typeof parsed.utterance_id === 'string'
          ) {
            this.audioCorrelation = {
              sessionId: parsed.session_id,
              utteranceId: parsed.utterance_id,
            }
          }
        }
        const frames = typeof data === 'string' ? mockBackend.textFrames : mockBackend.binaryFrames
        const correlation = this.audioCorrelation
        window.setTimeout(() => {
          for (const frame of frames) {
            this.dispatchMessage(createFrameData(frame, correlation))
          }
        }, 0)
      }

      close() {
        this.readyState = MockWebSocket.CLOSED
        const event = new CloseEvent('close')
        this.onclose?.(event)
        this.dispatchEvent(event)
      }

      private dispatchMessage(data: string | ArrayBuffer) {
        const event = new MessageEvent('message', { data })
        this.onmessage?.(event)
        this.dispatchEvent(event)
      }
    }

    window.WebSocket = MockWebSocket as unknown as typeof WebSocket
  }, backend)
}
