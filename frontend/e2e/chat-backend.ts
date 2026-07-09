export const CHAT_BACKEND_ENV = 'CHAT_E2E_BACKEND'
export const CHAT_BACKEND_ORIGIN_ENV = 'CHAT_E2E_BACKEND_ORIGIN'
const VOICE_CHAT_BACKEND_ENV = 'VOICE_CHAT_E2E_BACKEND'

export type ChatBackend = {
  mode: 'real' | 'mock'
  backendOrigin: string | null
  reasons: string[]
}

const resolveMockChatBackend = (): ChatBackend => ({
  mode: 'mock',
  backendOrigin: null,
  reasons: ['CHAT_E2E_BACKEND is unset or mock; mock backend requested'],
})

const resolveRealChatBackend = (): ChatBackend => {
  const voiceBackendMode = process.env[VOICE_CHAT_BACKEND_ENV]
  if (voiceBackendMode === 'mock') {
    throw new Error(
      'CHAT_E2E_BACKEND=real requires VOICE_CHAT_E2E_BACKEND to be unset or "real"; received "mock"',
    )
  }

  const backendOrigin = process.env[CHAT_BACKEND_ORIGIN_ENV]
  if (backendOrigin === undefined || backendOrigin.length === 0) {
    return {
      mode: 'real',
      backendOrigin: null,
      reasons: ['CHAT_E2E_BACKEND=real requested without CHAT_E2E_BACKEND_ORIGIN; real chat tests will skip'],
    }
  }

  return {
    mode: 'real',
    backendOrigin,
    reasons: ['CHAT_E2E_BACKEND=real requested with CHAT_E2E_BACKEND_ORIGIN evidence'],
  }
}

export const resolveChatBackend = (): ChatBackend => {
  const backendMode = process.env[CHAT_BACKEND_ENV]
  if (backendMode === undefined || backendMode === 'mock') {
    return resolveMockChatBackend()
  }

  if (backendMode === 'real') {
    return resolveRealChatBackend()
  }

  throw new Error(`${CHAT_BACKEND_ENV} must be "mock" or "real"; received "${backendMode}"`)
}

export const shouldSkipRealChatBackend = (chatBackend: ChatBackend): boolean => (
  chatBackend.mode === 'real' && chatBackend.backendOrigin === null
)
