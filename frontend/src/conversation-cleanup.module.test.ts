import { afterEach, describe, expect, test, vi } from 'vitest'

import { hardDeleteSelectedConversation } from '../playwright/conversation-cleanup'

const CHARACTER = 'miori'
const CONVERSATION_ID = '2a744066-56a4-41ff-b22a-3b1434400167'
const NEXT_CONVERSATION_ID = 'ad5f83c8-edc1-4855-a6e4-39eb048dc545'
const STORAGE_KEY = `digital-souls:conversation:${CHARACTER}`

const browserPage = {
  url: () => 'http://localhost:5173/',
  evaluate: async <Result, Argument>(
    callback: (argument: Argument) => Result | Promise<Result>,
    argument: Argument,
  ): Promise<Result> => callback(argument),
}

afterEach(() => {
  localStorage.clear()
  vi.unstubAllGlobals()
})

describe('integration conversation cleanup', () => {
  test('hard-deletes the selected conversation and clears its persisted selection', async () => {
    localStorage.setItem(STORAGE_KEY, CONVERSATION_ID)
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(hardDeleteSelectedConversation(browserPage, CHARACTER))
      .resolves.toBe(CONVERSATION_ID)
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/characters/${CHARACTER}/conversations/${CONVERSATION_ID}`,
      { method: 'DELETE' },
    )
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  test('does not issue a delete when the test did not create a conversation', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(hardDeleteSelectedConversation(browserPage, CHARACTER)).resolves.toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  test('preserves a newer selection made while the previous conversation is deleted', async () => {
    localStorage.setItem(STORAGE_KEY, CONVERSATION_ID)
    vi.stubGlobal('fetch', vi.fn(async () => {
      localStorage.setItem(STORAGE_KEY, NEXT_CONVERSATION_ID)
      return new Response(null, { status: 204 })
    }))

    await expect(hardDeleteSelectedConversation(browserPage, CHARACTER))
      .resolves.toBe(CONVERSATION_ID)
    expect(localStorage.getItem(STORAGE_KEY)).toBe(NEXT_CONVERSATION_ID)
  })

  test('does not access origin storage when capability checks skip before navigation', async () => {
    const skippedPage = { url: () => 'about:blank', evaluate: vi.fn() }

    await expect(hardDeleteSelectedConversation(skippedPage, CHARACTER)).resolves.toBeNull()
    expect(skippedPage.evaluate).not.toHaveBeenCalled()
  })

  test('reports cleanup failure and preserves the ID for a retry', async () => {
    localStorage.setItem(STORAGE_KEY, CONVERSATION_ID)
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 503 })))

    await expect(hardDeleteSelectedConversation(browserPage, CHARACTER))
      .rejects.toThrow('Conversation cleanup failed with status 503')
    expect(localStorage.getItem(STORAGE_KEY)).toBe(CONVERSATION_ID)
  })
})
