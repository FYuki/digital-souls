import { afterEach, expect, test, vi } from 'vitest'

import { WebSocketAudioTransport, type TransportCallbacks } from './lib/audio/transport'
import { sendChatMessage } from './lib/chat/client'
import { createConversationSessionManager } from './lib/conversation-session'

const CONVERSATION_ID_A = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const CONVERSATION_ID_B = '6ad9a610-02cc-4a41-b02e-503826f7292b'

class FakeWebSocket {
  static urls: string[] = []
  binaryType = ''
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null

  constructor(readonly url: string) {
    FakeWebSocket.urls = [...FakeWebSocket.urls, url]
  }

  send() {}
  close() {}
}

const callbacks: TransportCallbacks = {
  onTextMessage: vi.fn(), onAudioMessage: vi.fn(), onError: vi.fn(),
  onTransportError: vi.fn(), onOpen: vi.fn(), onClose: vi.fn(),
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  FakeWebSocket.urls = []
  localStorage.clear()
})

test('should propagate isolated session IDs through A to B to A HTTP and WebSocket calls', async () => {
  let bodies: Record<string, string>[] = []
  vi.stubGlobal('crypto', {
    randomUUID: vi.fn()
      .mockReturnValueOnce(CONVERSATION_ID_A)
      .mockReturnValueOnce(CONVERSATION_ID_B),
  })
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body)) as Record<string, string>
    bodies = [...bodies, body]
    return new Response(JSON.stringify({ character: body.character, response: '応答' }), { status: 200 })
  }))
  const manager = createConversationSessionManager()

  for (const [character, message] of [
    ['miori', 'first A message'],
    ['miori', 'second A message'],
    ['mock-character-b', 'B message'],
    ['miori', 'returned A message'],
  ] as const) {
    const conversationId = manager.getConversationId(character)
    await sendChatMessage({ character, conversationId, message })
    new WebSocketAudioTransport(
      `ws://backend.test/ws/${character}?token=kept`, conversationId, callbacks,
    ).connect()
  }

  const webSocketUrls = FakeWebSocket.urls.map((value) => new URL(value))
  expect(bodies.map((body) => body.character)).toEqual([
    'miori', 'miori', 'mock-character-b', 'miori',
  ])
  expect(bodies.map((body) => body.conversation_id)).toEqual([
    CONVERSATION_ID_A, CONVERSATION_ID_A, CONVERSATION_ID_B, CONVERSATION_ID_A,
  ])
  expect(webSocketUrls.map((url) => url.pathname)).toEqual([
    '/ws/miori', '/ws/miori', '/ws/mock-character-b', '/ws/miori',
  ])
  expect(webSocketUrls.map((url) => url.searchParams.get('conversation_id'))).toEqual(
    bodies.map((body) => body.conversation_id),
  )
  expect(webSocketUrls.every((url) => url.searchParams.get('token') === 'kept')).toBe(true)
})
