import { afterEach, describe, expect, test, vi } from 'vitest'

import { createConversationSessionManager } from './conversation-session'

const MIORI_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const SECOND_MIORI_ID = '62f217d0-9b14-40f8-8df3-dd6f4a7dc758'
const OTHER_ID = 'c7b12e47-a2cf-4af7-b503-e4f447ee03ad'

type StorageDouble = Storage & { values: Map<string, string> }

const createStorage = (): StorageDouble => {
  const values = new Map<string, string>()

  return {
    values,
    get length() {
      return values.size
    },
    clear: vi.fn(() => values.clear()),
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    key: vi.fn((index: number) => [...values.keys()][index] ?? null),
    removeItem: vi.fn((key: string) => values.delete(key)),
    setItem: vi.fn((key: string, value: string) => values.set(key, value)),
  }
}

const installEnvironment = (ids: string[], storage: Storage = createStorage()) => {
  let nextIdIndex = 0
  const randomUUID = vi.fn(() => {
    const id = ids[nextIdIndex]
    if (id === undefined) throw new Error('Unexpected UUID generation')
    nextIdIndex += 1
    return id
  })
  vi.stubGlobal('crypto', { randomUUID })
  vi.stubGlobal('localStorage', storage)
  return { randomUUID, storage }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('ConversationSessionManager', () => {
  test('should generate a UUIDv4 with crypto.randomUUID and persist it when no value exists', () => {
    const { randomUUID, storage } = installEnvironment([MIORI_ID])
    const manager = createConversationSessionManager()

    const conversationId = manager.getConversationId('miori')

    expect(conversationId).toBe(MIORI_ID)
    expect(conversationId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    )
    expect(randomUUID).toHaveBeenCalledTimes(1)
    expect(storage.setItem).toHaveBeenCalledWith(expect.any(String), MIORI_ID)
  })

  test('should restore a valid persisted ID for the same character without generating another ID', () => {
    const storage = createStorage()
    const firstEnvironment = installEnvironment([MIORI_ID], storage)
    createConversationSessionManager().getConversationId('miori')
    const persistedKey = vi.mocked(storage.setItem).mock.calls[0]?.[0]
    vi.unstubAllGlobals()
    const secondEnvironment = installEnvironment([], storage)

    const restoredId = createConversationSessionManager().getConversationId('miori')

    expect(persistedKey).toBeTruthy()
    expect(restoredId).toBe(MIORI_ID)
    expect(firstEnvironment.randomUUID).toHaveBeenCalledTimes(1)
    expect(secondEnvironment.randomUUID).not.toHaveBeenCalled()
  })

  test('should reuse one ID for repeated access to the same character', () => {
    const { randomUUID } = installEnvironment([MIORI_ID])
    const manager = createConversationSessionManager()

    const first = manager.getConversationId('miori')
    const second = manager.getConversationId('miori')

    expect(second).toBe(first)
    expect(randomUUID).toHaveBeenCalledTimes(1)
  })

  test('should isolate IDs and storage keys between characters and restore A after A to B to A', () => {
    const { storage } = installEnvironment([MIORI_ID, OTHER_ID])
    const manager = createConversationSessionManager()

    const firstA = manager.getConversationId('character/a')
    const characterB = manager.getConversationId('character b')
    const secondA = manager.getConversationId('character/a')

    const writtenKeys = vi.mocked(storage.setItem).mock.calls.map(([key]) => key)
    expect(firstA).toBe(MIORI_ID)
    expect(characterB).toBe(OTHER_ID)
    expect(secondA).toBe(MIORI_ID)
    expect(new Set(writtenKeys)).toHaveLength(2)
    expect(writtenKeys.some((key) => key.endsWith(encodeURIComponent('character/a')))).toBe(true)
    expect(writtenKeys.some((key) => key.endsWith(encodeURIComponent('character b')))).toBe(true)
  })

  test('should replace an invalid persisted value with a newly generated UUIDv4', () => {
    const storage = createStorage()
    installEnvironment([MIORI_ID], storage)
    createConversationSessionManager().getConversationId('miori')
    const key = vi.mocked(storage.setItem).mock.calls[0]?.[0]
    if (key === undefined) throw new Error('Storage key is required')
    storage.values.set(key, 'not-a-uuid')
    vi.unstubAllGlobals()
    const { randomUUID } = installEnvironment([SECOND_MIORI_ID], storage)

    const conversationId = createConversationSessionManager().getConversationId('miori')

    expect(conversationId).toBe(SECOND_MIORI_ID)
    expect(randomUUID).toHaveBeenCalledTimes(1)
    expect(storage.values.get(key)).toBe(SECOND_MIORI_ID)
  })

  test('should fail explicitly when crypto.randomUUID is unavailable', () => {
    vi.stubGlobal('crypto', {})
    vi.stubGlobal('localStorage', createStorage())
    const manager = createConversationSessionManager()

    expect(() => manager.getConversationId('miori')).toThrow(/randomUUID/)
  })

  test('should reject a non-v4 value returned by crypto.randomUUID without persisting it', () => {
    const storage = createStorage()
    installEnvironment(['e98d6c65-1ae9-3d6f-a8c8-d59b0ad09010'], storage)
    const manager = createConversationSessionManager()

    expect(() => manager.getConversationId('miori')).toThrow(/UUIDv4/)
    expect(storage.setItem).not.toHaveBeenCalled()
  })

  test.each(['read', 'write'] as const)(
    'should use character-scoped memory when localStorage %s throws',
    (failure) => {
      const storage = createStorage()
      if (failure === 'read') vi.mocked(storage.getItem).mockImplementation(() => { throw new Error('denied') })
      if (failure === 'write') vi.mocked(storage.setItem).mockImplementation(() => { throw new Error('denied') })
      const { randomUUID } = installEnvironment([MIORI_ID, OTHER_ID], storage)
      const manager = createConversationSessionManager()

      const firstA = manager.getConversationId('miori')
      const characterB = manager.getConversationId('other')
      const secondA = manager.getConversationId('miori')

      expect(firstA).toBe(MIORI_ID)
      expect(characterB).toBe(OTHER_ID)
      expect(secondA).toBe(MIORI_ID)
      expect(randomUUID).toHaveBeenCalledTimes(2)
    },
  )

  test('should reuse character-scoped memory when localStorage is unavailable', () => {
    const randomUUID = vi.fn(() => MIORI_ID)
    vi.stubGlobal('crypto', { randomUUID })
    vi.stubGlobal('localStorage', undefined)
    const manager = createConversationSessionManager()

    const first = manager.getConversationId('miori')
    const second = manager.getConversationId('miori')

    expect(first).toBe(MIORI_ID)
    expect(second).toBe(MIORI_ID)
    expect(randomUUID).toHaveBeenCalledTimes(1)
  })

  test('should use memory when acquiring localStorage throws', () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => MIORI_ID) })
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get: () => {
        throw new Error('storage getter denied')
      },
    })

    try {
      const manager = createConversationSessionManager()
      expect(manager.getConversationId('miori')).toBe(MIORI_ID)
      expect(manager.getConversationId('miori')).toBe(MIORI_ID)
    } finally {
      if (descriptor === undefined) {
        Reflect.deleteProperty(globalThis, 'localStorage')
      } else {
        Object.defineProperty(globalThis, 'localStorage', descriptor)
      }
    }
  })
})
