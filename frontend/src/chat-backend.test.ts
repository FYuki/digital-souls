import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  CHAT_BACKEND_ENV,
  CHAT_BACKEND_ORIGIN_ENV,
  type ChatBackend,
  resolveChatBackend,
  shouldSkipRealChatBackend,
} from '../e2e/chat-backend'

const expectResolvedChatBackend = (expected: ChatBackend) => {
  expect(resolveChatBackend()).toEqual(expected)
}

describe('chat backend env resolver', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  test('should use mock mode when chat backend mode is unset', () => {
    vi.stubEnv(CHAT_BACKEND_ENV, undefined)
    vi.stubEnv(CHAT_BACKEND_ORIGIN_ENV, 'http://127.0.0.1:18000')

    expectResolvedChatBackend({
      mode: 'mock',
      backendOrigin: null,
      reasons: ['CHAT_E2E_BACKEND is unset or mock; mock backend requested'],
    })
  })

  test('should use mock mode when chat backend mode is mock', () => {
    vi.stubEnv(CHAT_BACKEND_ENV, 'mock')
    vi.stubEnv(CHAT_BACKEND_ORIGIN_ENV, 'http://127.0.0.1:18000')

    expectResolvedChatBackend({
      mode: 'mock',
      backendOrigin: null,
      reasons: ['CHAT_E2E_BACKEND is unset or mock; mock backend requested'],
    })
  })

  test('should use real mode with backend origin for evidence', () => {
    vi.stubEnv(CHAT_BACKEND_ENV, 'real')
    vi.stubEnv(CHAT_BACKEND_ORIGIN_ENV, 'http://127.0.0.1:18000')
    vi.stubEnv('VOICE_CHAT_E2E_BACKEND', undefined)

    expectResolvedChatBackend({
      mode: 'real',
      backendOrigin: 'http://127.0.0.1:18000',
      reasons: ['CHAT_E2E_BACKEND=real requested with CHAT_E2E_BACKEND_ORIGIN evidence'],
    })
  })

  test('should use real mode without backend origin when origin env is unset', () => {
    vi.stubEnv(CHAT_BACKEND_ENV, 'real')
    vi.stubEnv(CHAT_BACKEND_ORIGIN_ENV, undefined)
    vi.stubEnv('VOICE_CHAT_E2E_BACKEND', undefined)

    expectResolvedChatBackend({
      mode: 'real',
      backendOrigin: null,
      reasons: ['CHAT_E2E_BACKEND=real requested without CHAT_E2E_BACKEND_ORIGIN; real chat tests will skip'],
    })
  })

  test('should skip real mode tests when backend origin is unset', () => {
    expect(shouldSkipRealChatBackend({
      mode: 'real',
      backendOrigin: null,
      reasons: [],
    })).toBe(true)
    expect(shouldSkipRealChatBackend({
      mode: 'real',
      backendOrigin: 'http://127.0.0.1:18000',
      reasons: [],
    })).toBe(false)
    expect(shouldSkipRealChatBackend({
      mode: 'mock',
      backendOrigin: null,
      reasons: [],
    })).toBe(false)
  })

  test('should fail fast when chat backend mode is invalid', () => {
    vi.stubEnv(CHAT_BACKEND_ENV, 'auto')

    expect(() => resolveChatBackend()).toThrow(
      'CHAT_E2E_BACKEND must be "mock" or "real"; received "auto"',
    )
  })

  test('should fail fast when chat real mode conflicts with voice mock startup', () => {
    vi.stubEnv(CHAT_BACKEND_ENV, 'real')
    vi.stubEnv(CHAT_BACKEND_ORIGIN_ENV, 'http://127.0.0.1:18000')
    vi.stubEnv('VOICE_CHAT_E2E_BACKEND', 'mock')

    expect(() => resolveChatBackend()).toThrow(
      'CHAT_E2E_BACKEND=real requires VOICE_CHAT_E2E_BACKEND to be unset or "real"; received "mock"',
    )
  })
})
