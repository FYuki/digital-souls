const STORAGE_KEY_PREFIX = 'digital-souls:conversation:'
const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export type ConversationSessionManager = {
  getConversationId: (character: string) => string
}

const createConversationId = (): string => {
  if (typeof globalThis.crypto?.randomUUID !== 'function') {
    throw new Error('crypto.randomUUID is required to create a conversation ID')
  }

  const conversationId = globalThis.crypto.randomUUID()
  if (!UUID_V4_PATTERN.test(conversationId)) {
    throw new Error('crypto.randomUUID returned an invalid UUIDv4')
  }

  return conversationId
}

const resolveStorage = (): Storage | null => {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    // ブラウザ設定によってgetter自体が拒否されても、実行中の会話は継続する必要がある。
    return null
  }
}

export const createConversationSessionManager = (): ConversationSessionManager => {
  const conversationIds = new Map<string, string>()
  let storage = resolveStorage()

  const readPersistedId = (storageKey: string): string | null => {
    if (storage === null) return null

    try {
      return storage.getItem(storageKey)
    } catch {
      // 一度拒否されたStorageへ繰り返しアクセスせず、以後は同じメモリ状態を使う。
      storage = null
      return null
    }
  }

  const persistId = (storageKey: string, conversationId: string): void => {
    if (storage === null) return

    try {
      storage.setItem(storageKey, conversationId)
    } catch {
      // 永続化だけが失敗した場合も、生成済みIDはメモリ上で再利用する。
      storage = null
    }
  }

  return {
    getConversationId(character: string): string {
      const inMemoryId = conversationIds.get(character)
      if (inMemoryId !== undefined) return inMemoryId

      const storageKey = `${STORAGE_KEY_PREFIX}${encodeURIComponent(character)}`
      const persistedId = readPersistedId(storageKey)
      if (persistedId !== null && UUID_V4_PATTERN.test(persistedId)) {
        conversationIds.set(character, persistedId)
        return persistedId
      }

      const conversationId = createConversationId()
      conversationIds.set(character, conversationId)
      persistId(storageKey, conversationId)
      return conversationId
    },
  }
}
