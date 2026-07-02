import type { Page } from '@playwright/test'

type MockWebSocketFrame =
  | { kind: 'text'; data: string }
  | { kind: 'binary'; data: number[] }

type MockWebSocketBackend = {
  textFrames: MockWebSocketFrame[]
  binaryFrames: MockWebSocketFrame[]
}

export const installMockWebSocketBackend = async (page: Page, backend: MockWebSocketBackend) => {
  await page.addInitScript((mockBackend) => {
    const createFrameData = (frame: MockWebSocketFrame): string | ArrayBuffer => {
      if (frame.kind === 'text') {
        return frame.data
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

      constructor(url: string | URL) {
        super()
        this.url = String(url)
        window.setTimeout(() => {
          this.readyState = MockWebSocket.OPEN
          const event = new Event('open')
          this.onopen?.(event)
          this.dispatchEvent(event)
        }, 0)
      }

      send(data: string | ArrayBufferLike | Blob | ArrayBufferView) {
        const frames = typeof data === 'string' ? mockBackend.textFrames : mockBackend.binaryFrames
        window.setTimeout(() => {
          for (const frame of frames) {
            this.dispatchMessage(createFrameData(frame))
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

    window.WebSocket = MockWebSocket as typeof WebSocket
  }, backend)
}
