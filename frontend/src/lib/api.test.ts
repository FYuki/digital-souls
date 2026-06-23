import { afterEach, describe, expect, test, vi } from 'vitest'

import { sendMessage } from './api'

describe('sendMessage', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  test('should post the BE chat request root shape through the Vite proxy when a message is sent', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ character: 'miori', response: 'おかえりなさい。' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const response = await sendMessage('ただいま')

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ character: 'miori', message: 'ただいま' }),
    })
    expect(response).toBe('おかえりなさい。')
  })

  test('should throw when the backend rejects the chat request', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Character 'miori' not found" }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    const result = sendMessage('こんにちは')

    await expect(result).rejects.toThrow()
  })

  test('should throw when the backend returns a different character', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ character: 'unknown', response: 'ただいま。' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )

    const result = sendMessage('こんにちは')

    await expect(result).rejects.toThrow('Chat response shape is invalid')
  })
})
