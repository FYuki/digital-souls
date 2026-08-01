import { afterEach, describe, expect, test, vi } from 'vitest'

import { sendChatMessage } from './client'

const CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'

const response = (body: unknown, init: ResponseInit = {}): Response =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
    ...init,
  })

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('sendChatMessage', () => {
  test('should post character, conversation_id, and message in the root JSON request body', async () => {
    const fetchMock = vi.fn<
      (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>
    >(async () => response({ character: 'miori', response: 'おかえりなさい。' }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await sendChatMessage({
      character: 'miori',
      conversationId: CONVERSATION_ID,
      message: 'ただいま',
    })

    expect(fetchMock).toHaveBeenCalledOnce()
    const request = fetchMock.mock.calls[0]
    if (request === undefined) throw new Error('Fetch request is required')
    const [url, init] = request
    expect(url).toBe('/api/chat')
    expect(init).toMatchObject({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    })
    expect(JSON.parse(String(init?.body))).toEqual({
      character: 'miori',
      conversation_id: CONVERSATION_ID,
      message: 'ただいま',
    })
    expect(result).toEqual({ character: 'miori', response: 'おかえりなさい。' })
  })

  test('should reject a non-success HTTP response', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ detail: 'failure' }, { status: 503 })))

    await expect(
      sendChatMessage({ character: 'miori', conversationId: CONVERSATION_ID, message: '応答して' }),
    ).rejects.toThrow(/503/)
  })

  test.each([
    null,
    { character: 'miori' },
    { response: '本文のみ' },
    { character: 1, response: '不正です' },
    { character: 'miori', response: 1 },
  ])('should reject an invalid success response shape: %j', async (body) => {
    vi.stubGlobal('fetch', vi.fn(async () => response(body)))

    await expect(
      sendChatMessage({ character: 'miori', conversationId: CONVERSATION_ID, message: '応答して' }),
    ).rejects.toThrow(/response/i)
  })

  test('should reject a response for a different character', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({ character: 'other', response: '混入' })))

    await expect(
      sendChatMessage({ character: 'miori', conversationId: CONVERSATION_ID, message: '応答して' }),
    ).rejects.toThrow(/character/i)
  })
})
