import { afterEach, describe, expect, test, vi } from 'vitest'

import { createConversationSessionManager } from './conversation-session'

const MIORI_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
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

const installStorage = (storage: Storage = createStorage()) => {
  vi.stubGlobal('localStorage', storage)
  return storage
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('ConversationSessionManager', () => {
  test('should persist and return a selected conversation for one character', () => {
    const storage = installStorage()
    const manager = createConversationSessionManager()

    manager.selectConversation('miori', MIORI_ID)

    expect(manager.getSelectedConversationId('miori')).toBe(MIORI_ID)
    expect(storage.setItem).toHaveBeenCalledWith(expect.any(String), MIORI_ID)
  })

  test('should restore a valid persisted selection without generating an ID', () => {
    const storage = installStorage()
    createConversationSessionManager().selectConversation('miori', MIORI_ID)

    const restoredId = createConversationSessionManager().getSelectedConversationId('miori')

    expect(restoredId).toBe(MIORI_ID)
  })

  test('should isolate selected IDs and storage keys between characters', () => {
    const storage = installStorage()
    const manager = createConversationSessionManager()

    manager.selectConversation('character/a', MIORI_ID)
    manager.selectConversation('character b', OTHER_ID)

    const writtenKeys = vi.mocked(storage.setItem).mock.calls.map(([key]) => key)
    expect(manager.getSelectedConversationId('character/a')).toBe(MIORI_ID)
    expect(manager.getSelectedConversationId('character b')).toBe(OTHER_ID)
    expect(new Set(writtenKeys)).toHaveLength(2)
    expect(writtenKeys.some((key) => key.endsWith(encodeURIComponent('character/a')))).toBe(true)
    expect(writtenKeys.some((key) => key.endsWith(encodeURIComponent('character b')))).toBe(true)
  })

  test('should ignore an invalid persisted selection', () => {
    const storage = createStorage()
    storage.values.set('digital-souls:conversation:miori', 'not-a-uuid')
    installStorage(storage)

    const selectedId = createConversationSessionManager().getSelectedConversationId('miori')

    expect(selectedId).toBeNull()
  })

  test('should reject a non-v4 selection without persisting it', () => {
    const storage = installStorage()
    const manager = createConversationSessionManager()

    expect(() => manager.selectConversation('miori', 'not-a-uuid')).toThrow(/UUIDv4/)
    expect(storage.setItem).not.toHaveBeenCalled()
  })

  test.each(['read', 'write'] as const)(
    'should retain an explicit selection in memory when localStorage %s throws',
    (failure) => {
      const storage = createStorage()
      if (failure === 'read') vi.mocked(storage.getItem).mockImplementation(() => { throw new Error('denied') })
      if (failure === 'write') vi.mocked(storage.setItem).mockImplementation(() => { throw new Error('denied') })
      installStorage(storage)
      const manager = createConversationSessionManager()
      if (failure === 'read') expect(manager.getSelectedConversationId('miori')).toBeNull()

      manager.selectConversation('miori', MIORI_ID)

      expect(manager.getSelectedConversationId('miori')).toBe(MIORI_ID)
    },
  )

  test('should retain an explicit selection in memory when localStorage is unavailable', () => {
    vi.stubGlobal('localStorage', undefined)
    const manager = createConversationSessionManager()

    manager.selectConversation('miori', MIORI_ID)

    expect(manager.getSelectedConversationId('miori')).toBe(MIORI_ID)
  })

  test('should retain an explicit selection in memory when acquiring localStorage throws', () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get: () => {
        throw new Error('storage getter denied')
      },
    })

    try {
      const manager = createConversationSessionManager()
      manager.selectConversation('miori', MIORI_ID)
      expect(manager.getSelectedConversationId('miori')).toBe(MIORI_ID)
    } finally {
      if (descriptor === undefined) {
        Reflect.deleteProperty(globalThis, 'localStorage')
      } else {
        Object.defineProperty(globalThis, 'localStorage', descriptor)
      }
    }
  })

  test('should remove the matching selected conversation from memory and storage', () => {
    const storage = installStorage()
    const manager = createConversationSessionManager()
    manager.selectConversation('miori', MIORI_ID)

    manager.clearConversation('miori', MIORI_ID)

    expect(manager.getSelectedConversationId('miori')).toBeNull()
    expect(storage.removeItem).toHaveBeenCalledTimes(1)
  })

  test('should retain a selected conversation when clearing another conversation', () => {
    const storage = installStorage()
    const manager = createConversationSessionManager()
    manager.selectConversation('miori', MIORI_ID)

    manager.clearConversation('miori', OTHER_ID)

    expect(manager.getSelectedConversationId('miori')).toBe(MIORI_ID)
    expect(storage.removeItem).not.toHaveBeenCalled()
  })
})
