import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { WebSocketAudioTransport, type TransportCallbacks } from './transport'

const AUDIO_WS_URL = 'ws://backend.test/ws/miori'

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  static failNextSend = false

  binaryType = ''
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  sent: (string | ArrayBuffer)[] = []
  closed = false

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this)
  }

  send(data: string | ArrayBuffer) {
    if (FakeWebSocket.failNextSend) {
      FakeWebSocket.failNextSend = false
      throw new Error('WebSocket send failed')
    }

    this.sent.push(data)
  }

  close() {
    this.closed = true
    this.onclose?.()
  }
}

const createCallbacks = (): TransportCallbacks => ({
  onTextMessage: vi.fn(),
  onAudioMessage: vi.fn(),
  onError: vi.fn(),
  onTransportError: vi.fn(),
  onOpen: vi.fn(),
  onClose: vi.fn(),
})

const connectTransport = async (callbacks: TransportCallbacks) => {
  const transport = new WebSocketAudioTransport(AUDIO_WS_URL, callbacks)
  const connection = transport.connect()
  const socket = FakeWebSocket.instances[0]

  if (socket === undefined) {
    throw new Error('WebSocket instance is required')
  }

  socket.onopen?.()
  await connection

  return { transport, socket }
}

describe('WebSocketAudioTransport', () => {
  beforeEach(() => {
    FakeWebSocket.instances = []
    FakeWebSocket.failNextSend = false
    vi.stubGlobal('WebSocket', FakeWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  test('should connect to the injected Miori WebSocket endpoint', async () => {
    const callbacks = createCallbacks()

    const { transport, socket } = await connectTransport(callbacks)

    expect(socket.url).toBe(AUDIO_WS_URL)
    expect(socket.binaryType).toBe('arraybuffer')
    expect(transport.connected).toBe(true)
    expect(callbacks.onOpen).toHaveBeenCalledTimes(1)
  })

  test('should send audio as a binary frame without a JSON envelope', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)
    const pcm = new ArrayBuffer(4)

    transport.sendAudio(pcm)

    expect(socket.sent).toEqual([pcm])
  })

  test('should send text through the same WebSocket as a text message frame', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)

    transport.sendText('ただいま')

    expect(socket.sent).toEqual([JSON.stringify({ type: 'text', message: 'ただいま' })])
  })

  test('should route legacy backend text messages as miori replies', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)

    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', response: 'おかえりなさい。' }),
      }),
    )
    expect(callbacks.onTextMessage).toHaveBeenCalledWith('miori', 'おかえりなさい。')
  })

  test('should route user speaker text frames with the message field', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)

    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', speaker: 'user', message: '今日は晴れています' }),
      }),
    )

    expect(callbacks.onTextMessage).toHaveBeenCalledWith('user', '今日は晴れています')
  })

  test('should route miori speaker text frames with the response field', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)

    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', speaker: 'miori', response: '散歩日和ですね。' }),
      }),
    )

    expect(callbacks.onTextMessage).toHaveBeenCalledWith('miori', '散歩日和ですね。')
  })

  test('should reject miori speaker text frames without a response field', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)

    expect(() => {
      socket.onmessage?.(
        new MessageEvent('message', {
          data: JSON.stringify({ type: 'text', speaker: 'miori', message: '取り違えた本文' }),
        }),
      )
    }).toThrow('WebSocket message shape is invalid')
  })

  test('should reject user speaker text frames without a message field', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)

    expect(() => {
      socket.onmessage?.(
        new MessageEvent('message', {
          data: JSON.stringify({ type: 'text', speaker: 'user', response: '取り違えた本文' }),
        }),
      )
    }).toThrow('WebSocket message shape is invalid')
  })

  test('should route WAV binary messages to the audio callback', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)
    const pcm = new ArrayBuffer(2)
    const wav = new ArrayBuffer(8)

    transport.sendAudio(pcm)
    socket.onmessage?.(new MessageEvent('message', { data: wav }))

    expect(callbacks.onAudioMessage).toHaveBeenCalledWith(wav)
  })

  test('should convert Blob binary messages before routing them to the audio callback', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)
    const wavBytes = new Uint8Array([1, 2, 3, 4])

    socket.onmessage?.(new MessageEvent('message', { data: new Blob([wavBytes]) }))

    await vi.waitFor(() => {
      expect(callbacks.onAudioMessage).toHaveBeenCalledWith(wavBytes.buffer)
    })
  })

  test('should route backend error messages to the error callback', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)

    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'error', status: 404, detail: 'Character not found' }),
      }),
    )

    expect(callbacks.onError).toHaveBeenCalledWith({ status: 404, detail: 'Character not found' })
  })

  test('should keep text frame routing independent from repeated audio sends', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)
    const firstPcm = new ArrayBuffer(2)
    const secondPcm = new ArrayBuffer(4)
    const wav = new ArrayBuffer(8)

    transport.sendAudio(firstPcm)
    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', speaker: 'user', message: '音声の質問' }),
      }),
    )
    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', speaker: 'miori', response: '音声の応答です。' }),
      }),
    )

    transport.sendAudio(secondPcm)

    socket.onmessage?.(new MessageEvent('message', { data: wav }))

    expect(callbacks.onTextMessage).toHaveBeenNthCalledWith(1, 'user', '音声の質問')
    expect(callbacks.onTextMessage).toHaveBeenNthCalledWith(2, 'miori', '音声の応答です。')
    expect(callbacks.onAudioMessage).toHaveBeenCalledWith(wav)
    expect(socket.sent).toEqual([firstPcm, secondPcm])
  })

  test('should reject broken JSON text frames', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)

    expect(() => {
      socket.onmessage?.(new MessageEvent('message', { data: '{' }))
    }).toThrow('WebSocket message shape is invalid')
  })

  test('should reject unknown text frame types', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)

    expect(() => {
      socket.onmessage?.(
        new MessageEvent('message', {
          data: JSON.stringify({ type: 'unknown', text: '未定義です' }),
        }),
      )
    }).toThrow('WebSocket message shape is invalid')
  })

  test('should route audio responses without owning pending state', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)
    const wav = new ArrayBuffer(8)

    socket.onmessage?.(new MessageEvent('message', { data: wav }))

    expect(callbacks.onAudioMessage).toHaveBeenCalledWith(wav)
  })

  test('should allow repeated audio sends because App owns audio pending state', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)
    const firstPcm = new ArrayBuffer(2)
    const secondPcm = new ArrayBuffer(4)

    transport.sendAudio(firstPcm)
    transport.sendAudio(secondPcm)

    expect(socket.sent).toEqual([firstPcm, secondPcm])
  })

  test('should keep audio sending available when an error frame arrives', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)
    const firstPcm = new ArrayBuffer(2)
    const secondPcm = new ArrayBuffer(4)

    transport.sendAudio(firstPcm)
    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'error', status: 502, detail: 'STT request failed' }),
      }),
    )
    transport.sendAudio(secondPcm)

    expect(socket.sent).toEqual([firstPcm, secondPcm])
    expect(callbacks.onError).toHaveBeenCalledWith({ status: 502, detail: 'STT request failed' })
  })

  test('should notify transport runtime errors after the socket has opened', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)
    const firstPcm = new ArrayBuffer(2)
    const secondPcm = new ArrayBuffer(4)

    transport.sendAudio(firstPcm)
    socket.onerror?.()
    transport.sendAudio(secondPcm)

    expect(callbacks.onTransportError).toHaveBeenCalledWith(expect.any(Error))
    expect(callbacks.onError).not.toHaveBeenCalled()
    expect(socket.sent).toEqual([firstPcm, secondPcm])
  })

  test('should keep audio sending available after reconnecting a closed socket', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)
    const secondPcm = new ArrayBuffer(4)

    transport.sendAudio(new ArrayBuffer(2))
    socket.onclose?.()
    const connection = transport.connect()
    const reconnectedSocket = FakeWebSocket.instances[1]

    if (reconnectedSocket === undefined) {
      throw new Error('Reconnected WebSocket instance is required')
    }

    reconnectedSocket.onopen?.()
    await connection
    transport.sendAudio(secondPcm)

    expect(transport.connected).toBe(true)
    expect(reconnectedSocket.sent).toEqual([secondPcm])
    expect(callbacks.onClose).toHaveBeenCalledTimes(1)
  })

  test('should fail fast when sending audio before the socket is open', () => {
    const transport = new WebSocketAudioTransport(AUDIO_WS_URL, createCallbacks())

    expect(() => transport.sendAudio(new ArrayBuffer(2))).toThrow('WebSocket is not connected')
  })

  test('should fail fast when sending text before the socket is open', () => {
    const transport = new WebSocketAudioTransport(AUDIO_WS_URL, createCallbacks())

    expect(() => transport.sendText('未接続です')).toThrow('WebSocket is not connected')
  })

  test('should not keep audio pending when sending before the socket is open', async () => {
    const callbacks = createCallbacks()
    const transport = new WebSocketAudioTransport(AUDIO_WS_URL, callbacks)
    const connection = transport.connect()
    const socket = FakeWebSocket.instances[0]

    if (socket === undefined) {
      throw new Error('WebSocket instance is required')
    }

    expect(() => transport.sendAudio(new ArrayBuffer(2))).toThrow('WebSocket is not connected')

    socket.onopen?.()
    await connection
    transport.sendAudio(new ArrayBuffer(4))

    expect(socket.sent).toHaveLength(1)
  })

  test('should allow retrying audio when WebSocket send throws', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)
    const secondPcm = new ArrayBuffer(4)

    FakeWebSocket.failNextSend = true

    expect(() => transport.sendAudio(new ArrayBuffer(2))).toThrow('WebSocket send failed')

    transport.sendAudio(secondPcm)

    expect(socket.sent).toEqual([secondPcm])
  })

  test('should disconnect the active socket', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)

    transport.disconnect()

    expect(socket.closed).toBe(true)
    expect(transport.connected).toBe(false)
    expect(callbacks.onClose).toHaveBeenCalledTimes(1)
  })
})
