import type { Page } from '@playwright/test'

const STORAGE_KEY_PREFIX = 'digital-souls:conversation:'

export const hardDeleteSelectedConversation = async (
  page: Pick<Page, 'evaluate' | 'url'>,
  character: string,
): Promise<string | null> => {
  if (page.url() === 'about:blank') return null
  return page.evaluate(async ({ character, storageKey }) => {
    const conversationId = localStorage.getItem(storageKey)
    if (conversationId === null) return null

    const response = await fetch(
      `/api/characters/${encodeURIComponent(character)}/conversations/${conversationId}`,
      { method: 'DELETE' },
    )
    if (response.status !== 204) {
      throw new Error(`Conversation cleanup failed with status ${response.status}`)
    }
    if (localStorage.getItem(storageKey) === conversationId) {
      localStorage.removeItem(storageKey)
    }
    return conversationId
  }, {
    character,
    storageKey: `${STORAGE_KEY_PREFIX}${encodeURIComponent(character)}`,
  })
}
