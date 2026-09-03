import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  archiveConversation,
  createConversation,
  listActiveConversations,
  listArchivedConversations,
  renameConversation,
  unarchiveConversation,
} from './client'

const CHARACTER = 'miori'
const CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const OTHER_CONVERSATION_ID = '6ad9a610-02cc-4a41-b02e-503826f7292b'

const conversation = (
  characterId: string,
  conversationId: string,
  archivedAt: string | null,
) => ({
  character_id: characterId,
  conversation_id: conversationId,
  created_at: '2026-08-01T12:00:00+00:00',
  updated_at: '2026-08-01T12:01:00+00:00',
  archived_at: archivedAt,
  title: '新しい会話',
})

const respondWith = (body: unknown): void => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(
    JSON.stringify(body),
    { status: 200 },
  )))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('conversation lifecycle client response boundary', () => {
  test('should return a matching active list item when the response satisfies the request', async () => {
    const expected = conversation(CHARACTER, CONVERSATION_ID, null)
    respondWith([expected])

    const result = await listActiveConversations(CHARACTER)

    expect(result).toEqual([expected])
  })

  test('should accept a title containing 40 astral Unicode characters', async () => {
    const expected = {
      ...conversation(CHARACTER, CONVERSATION_ID, null),
      title: '😀'.repeat(40),
    }
    respondWith([expected])

    await expect(listActiveConversations(CHARACTER)).resolves.toEqual([expected])
  })

  test('should reject an active list item for another character', async () => {
    respondWith([conversation('akira', CONVERSATION_ID, null)])

    const request = listActiveConversations(CHARACTER)

    await expect(request).rejects.toThrow()
  })

  test('should reject an archived item from the active list', async () => {
    respondWith([conversation(CHARACTER, CONVERSATION_ID, '2026-08-01T13:00:00+00:00')])

    const request = listActiveConversations(CHARACTER)

    await expect(request).rejects.toThrow()
  })

  test('should reject an active item from the archived list', async () => {
    respondWith([conversation(CHARACTER, CONVERSATION_ID, null)])

    const request = listArchivedConversations(CHARACTER)

    await expect(request).rejects.toThrow()
  })

  test('should reject an archived create response', async () => {
    respondWith(conversation(CHARACTER, CONVERSATION_ID, '2026-08-01T13:00:00+00:00'))

    const request = createConversation(CHARACTER)

    await expect(request).rejects.toThrow()
  })

  test('should reject a create response for another character', async () => {
    respondWith(conversation('akira', CONVERSATION_ID, null))

    const request = createConversation(CHARACTER)

    await expect(request).rejects.toThrow()
  })

  test('should return a matching archived transition when the response satisfies the request', async () => {
    const expected = conversation(
      CHARACTER,
      CONVERSATION_ID,
      '2026-08-01T13:00:00+00:00',
    )
    respondWith(expected)

    const result = await archiveConversation(CHARACTER, CONVERSATION_ID)

    expect(result).toEqual(expected)
  })

  test('should reject an archive response for another conversation', async () => {
    respondWith(conversation(CHARACTER, OTHER_CONVERSATION_ID, '2026-08-01T13:00:00+00:00'))

    const request = archiveConversation(CHARACTER, CONVERSATION_ID)

    await expect(request).rejects.toThrow()
  })

  test('should reject an archive response that remains active', async () => {
    respondWith(conversation(CHARACTER, CONVERSATION_ID, null))

    const request = archiveConversation(CHARACTER, CONVERSATION_ID)

    await expect(request).rejects.toThrow()
  })

  test('should reject an archive response for another character', async () => {
    respondWith(conversation('akira', CONVERSATION_ID, '2026-08-01T13:00:00+00:00'))

    const request = archiveConversation(CHARACTER, CONVERSATION_ID)

    await expect(request).rejects.toThrow()
  })

  test('should reject an unarchive response that remains archived', async () => {
    respondWith(conversation(CHARACTER, CONVERSATION_ID, '2026-08-01T13:00:00+00:00'))

    const request = unarchiveConversation(CHARACTER, CONVERSATION_ID)

    await expect(request).rejects.toThrow()
  })

  test('should send a conversation title change through PATCH', async () => {
    const renamed = {
      ...conversation(CHARACTER, CONVERSATION_ID, null),
      title: '手動タイトル',
    }
    respondWith(renamed)

    const result = await renameConversation(
      CHARACTER,
      CONVERSATION_ID,
      '手動タイトル',
    )

    expect(result).toEqual(renamed)
    expect(fetch).toHaveBeenCalledWith(
      `/api/characters/${CHARACTER}/conversations/${CONVERSATION_ID}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: '手動タイトル' }),
      },
    )
  })
})
