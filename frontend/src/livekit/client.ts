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

const requestToken = async (
  conversationId: string,
  sessionId?: string,
): Promise<TokenResponse> => {
  const body: TokenRequest = {
    protocol_version: '1.0',
    request_id: crypto.randomUUID(),
    character_id: 'miori',
    conversation_id: conversationId,
    requested_reconnect_grace_ms: 60000,
  }
  if (sessionId !== undefined) body.session_id = sessionId
  const response = await fetch('/voice/livekit/token', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`LiveKit token request failed: ${response.status}`)
  return await response.json() as TokenResponse
}

export type ConversationBinding = Readonly<{
  character_id: string
  conversation_id: string
}>

export const createConversation = async (): Promise<ConversationBinding> => {
  const response = await fetch('/characters/miori/conversations', { method: 'POST' })
  if (!response.ok) throw new Error(`Conversation creation failed: ${response.status}`)
  return await response.json() as ConversationBinding
}

export const getInitialToken = async (): Promise<{
  conversationId: string
  token: TokenResponse
}> => {
  const conversation = await createConversation()
  return {
    conversationId: conversation.conversation_id,
    token: await requestToken(conversation.conversation_id),
  }
}
export const getReconnectToken = (
  conversationId: string,
  sessionId: string,
): Promise<TokenResponse> => requestToken(conversationId, sessionId)
