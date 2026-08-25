import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { WebSocketAudioTransport, type TransportCallbacks } from './transport'

const AUDIO_WS_URL = 'ws://backend.test/ws/miori'
const CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const SESSION_ID = '01992f57-8c65-79d0-924f-e2cd79bc01cd'

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

type MeasurementCallbacks = TransportCallbacks & {
  onAudioResponseMetadata: ReturnType<typeof vi.fn>
}

const createCallbacks = (): MeasurementCallbacks => ({
  onTurnMessage: vi.fn(),
  onAudioMessage: vi.fn(),
  onAudioResponseMetadata: vi.fn(),
  onError: vi.fn(),
  onTransportError: vi.fn(),
  onOpen: vi.fn(),
  onClose: vi.fn(),
})

const connectTransport = async (callbacks: TransportCallbacks) => {
  const transport = new WebSocketAudioTransport(AUDIO_WS_URL, CONVERSATION_ID, callbacks)
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

    expect(socket.url).toBe(`${AUDIO_WS_URL}?conversation_id=${CONVERSATION_ID}`)
    expect(socket.binaryType).toBe('arraybuffer')
    expect(transport.connected).toBe(true)
    expect(callbacks.onOpen).toHaveBeenCalledTimes(1)
  })

  test('should preserve existing query parameters when adding the conversation ID', () => {
    const transport = new WebSocketAudioTransport(
      `${AUDIO_WS_URL}?token=a%2Fb&mode=voice`,
      CONVERSATION_ID,
      createCallbacks(),
    )

    void transport.connect()

    const socket = FakeWebSocket.instances[0]
    if (socket === undefined) throw new Error('WebSocket instance is required')
    const url = new URL(socket.url)
    expect(url.searchParams.get('conversation_id')).toBe(CONVERSATION_ID)
    expect(url.searchParams.get('token')).toBe('a/b')
    expect(url.searchParams.get('mode')).toBe('voice')
  })

  test('should replace a stale conversation_id query parameter with the injected ID', () => {
    const transport = new WebSocketAudioTransport(
      `${AUDIO_WS_URL}?conversation_id=stale&token=kept`,
      CONVERSATION_ID,
      createCallbacks(),
    )

    void transport.connect()

    const socket = FakeWebSocket.instances[0]
    if (socket === undefined) throw new Error('WebSocket instance is required')
    const url = new URL(socket.url)
    expect(url.searchParams.getAll('conversation_id')).toEqual([CONVERSATION_ID])
    expect(url.searchParams.get('token')).toBe('kept')
  })

  test('should correlate audio metadata with the following binary frame', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)
    const pcm = new ArrayBuffer(4)

    const sendCorrelatedAudio = transport.sendAudio.bind(transport) as unknown as (
      data: ArrayBuffer,
      metadata: { eventId: string; sessionId: string; utteranceId: string },
    ) => void
    sendCorrelatedAudio(pcm, {
      eventId: '01992f57-8c65-79d0-924f-e2cd79bc03ef',
      sessionId: SESSION_ID,
      utteranceId: '01992f57-8c65-79d0-924f-e2cd79bc02de',
    })

    expect(socket.sent).toEqual([
      JSON.stringify({
        type: 'audio_metadata',
        event_id: '01992f57-8c65-79d0-924f-e2cd79bc03ef',
        session_id: SESSION_ID,
        utterance_id: '01992f57-8c65-79d0-924f-e2cd79bc02de',
      }),
      pcm,
    ])
  })

  test('should deliver response correlation before the WAV payload', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)
    const wav = new ArrayBuffer(8)

    socket.onmessage?.(new MessageEvent('message', {
      data: JSON.stringify({
        type: 'audio_response_metadata',
        session_id: SESSION_ID,
        utterance_id: '01992f57-8c65-79d0-924f-e2cd79bc02de',
        response_id: '01992f57-8c65-79d0-924f-e2cd79bc04fa',
      }),
    }))
    socket.onmessage?.(new MessageEvent('message', { data: wav }))

    expect(callbacks.onAudioResponseMetadata).toHaveBeenCalledWith({
      sessionId: SESSION_ID,
      utteranceId: '01992f57-8c65-79d0-924f-e2cd79bc02de',
      responseId: '01992f57-8c65-79d0-924f-e2cd79bc04fa',
    })
    expect(callbacks.onAudioMessage).toHaveBeenCalledWith(wav)
    expect(callbacks.onAudioResponseMetadata.mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(callbacks.onAudioMessage).mock.invocationCallOrder[0],
    )
  })

  test('should send playback measurements with one client clock domain', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)

    transport.sendMeasurementEvent({
      eventId: '01992f57-8c65-79d0-924f-e2cd79bc05ab',
      sessionId: SESSION_ID,
      utteranceId: '01992f57-8c65-79d0-924f-e2cd79bc02de',
      responseId: '01992f57-8c65-79d0-924f-e2cd79bc04fa',
      name: 'first_playback',
      timestamp: 1234.5,
    })

    expect(socket.sent).toEqual([JSON.stringify({
      type: 'measurement_event',
      event_id: '01992f57-8c65-79d0-924f-e2cd79bc05ab',
      session_id: SESSION_ID,
      utterance_id: '01992f57-8c65-79d0-924f-e2cd79bc02de',
      response_id: '01992f57-8c65-79d0-924f-e2cd79bc04fa',
      name: 'first_playback',
      timestamp: 1234.5,
      clock_domain: 'client_monotonic',
      unit: 'millisecond',
    })])
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

  test('should route persisted turns independently from repeated audio sends', async () => {
    const callbacks = createCallbacks()
    const { transport, socket } = await connectTransport(callbacks)
    const firstPcm = new ArrayBuffer(2)
    const secondPcm = new ArrayBuffer(4)
    const wav = new ArrayBuffer(8)

    transport.sendAudio(firstPcm)
    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({
          type: 'text',
          turn: {
            kind: 'content',
            turn_id: '9e70795d-e5d5-431d-baa2-67f884403010',
            user_content: '音声の質問',
            assistant_content: '音声の応答です。',
          },
        }),
      }),
    )

    transport.sendAudio(secondPcm)

    socket.onmessage?.(new MessageEvent('message', { data: wav }))

    expect(callbacks.onTurnMessage).toHaveBeenCalledWith({
      kind: 'content',
      turn_id: '9e70795d-e5d5-431d-baa2-67f884403010',
      user_content: '音声の質問',
      assistant_content: '音声の応答です。',
    })
    expect(callbacks.onAudioMessage).toHaveBeenCalledWith(wav)
    expect(socket.sent).toEqual([firstPcm, secondPcm])
  })

  test('should reject legacy text frames that are not persisted turns', async () => {
    const callbacks = createCallbacks()
    const { socket } = await connectTransport(callbacks)

    expect(() => {
      socket.onmessage?.(
        new MessageEvent('message', {
          data: JSON.stringify({ type: 'text', speaker: 'miori', response: '未保存の応答' }),
        }),
      )
    }).toThrow('WebSocket message shape is invalid')
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
    expect(new URL(reconnectedSocket.url).searchParams.get('conversation_id')).toBe(CONVERSATION_ID)
    expect(callbacks.onClose).toHaveBeenCalledTimes(1)
  })

  test('should fail fast when sending audio before the socket is open', () => {
    const transport = new WebSocketAudioTransport(AUDIO_WS_URL, CONVERSATION_ID, createCallbacks())

    expect(() => transport.sendAudio(new ArrayBuffer(2))).toThrow('WebSocket is not connected')
  })

  test('should not keep audio pending when sending before the socket is open', async () => {
    const callbacks = createCallbacks()
    const transport = new WebSocketAudioTransport(AUDIO_WS_URL, CONVERSATION_ID, callbacks)
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
