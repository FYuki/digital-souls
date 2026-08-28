export type TokenResponse = Readonly<{
  session_id: string
  participant_id: string
  room: string
  token: string
  livekit_url: string
  expires_at: string
  reconnect_grace_ms: number
}>

type TokenRequest = {
  protocol_version: '1.0'
  request_id: string
  character_id: string
  conversation_id: string
  requested_reconnect_grace_ms: 60000
  session_id?: string
}

const CHARACTER_ID = 'miori'
const API_PREFIX = '/api'

const requiredString = (value: unknown, field: string): string => {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`LiveKit response field ${field} must be a non-empty string`)
  }
  return value
}

const parseTokenResponse = (value: unknown): TokenResponse => {
  if (typeof value !== 'object' || value === null) {
    throw new Error('LiveKit token response must be an object')
  }
  const response = value as Record<string, unknown>
  const reconnectGraceMs = response.reconnect_grace_ms
  if (!Number.isInteger(reconnectGraceMs) || Number(reconnectGraceMs) < 0) {
    throw new Error('LiveKit response field reconnect_grace_ms must be a non-negative integer')
  }
  const expiresAt = requiredString(response.expires_at, 'expires_at')
  if (Number.isNaN(Date.parse(expiresAt))) {
    throw new Error('LiveKit response field expires_at must be an ISO timestamp')
  }
  return {
    session_id: requiredString(response.session_id, 'session_id'),
    participant_id: requiredString(response.participant_id, 'participant_id'),
    room: requiredString(response.room, 'room'),
    token: requiredString(response.token, 'token'),
    livekit_url: requiredString(response.livekit_url, 'livekit_url'),
    expires_at: expiresAt,
    reconnect_grace_ms: Number(reconnectGraceMs),
  }
}

const parseConversationBinding = (value: unknown): ConversationBinding => {
  if (typeof value !== 'object' || value === null) {
    throw new Error('Conversation response must be an object')
  }
  const response = value as Record<string, unknown>
  return {
    character_id: requiredString(response.character_id, 'character_id'),
    conversation_id: requiredString(response.conversation_id, 'conversation_id'),
  }
}

export const requestLiveKitToken = async (
  characterId: string,
  conversationId: string,
  sessionId?: string,
): Promise<TokenResponse> => {
  const body: TokenRequest = {
    protocol_version: '1.0',
    request_id: crypto.randomUUID(),
    character_id: characterId,
    conversation_id: conversationId,
    requested_reconnect_grace_ms: 60000,
  }
  if (sessionId !== undefined) body.session_id = sessionId
  const response = await fetch(`${API_PREFIX}/voice/livekit/token`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`LiveKit token request failed: ${response.status}`)
  return parseTokenResponse(await response.json() as unknown)
}

export type ConversationBinding = Readonly<{
  character_id: string
  conversation_id: string
}>

export const createConversation = async (): Promise<ConversationBinding> => {
  const response = await fetch(`${API_PREFIX}/characters/${CHARACTER_ID}/conversations`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error(`Conversation creation failed: ${response.status}`)
  return parseConversationBinding(await response.json() as unknown)
}

export const getInitialToken = async (): Promise<{
  conversationId: string
  token: TokenResponse
}> => {
  const conversation = await createConversation()
  return {
    conversationId: conversation.conversation_id,
    token: await requestLiveKitToken(CHARACTER_ID, conversation.conversation_id),
  }
}
export const getReconnectToken = (
  conversationId: string,
  sessionId: string,
): Promise<TokenResponse> => requestLiveKitToken(
  CHARACTER_ID,
  conversationId,
  sessionId,
)

export const endLiveKitSession = async (sessionId: string): Promise<void> => {
  const response = await fetch(
    `${API_PREFIX}/voice/livekit/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: 'DELETE',
    },
  )
  if (!response.ok && response.status !== 404) {
    throw new Error(`LiveKit session end failed: ${response.status}`)
  }
}
