import { CONVERSATION_ID_FIELD } from '../conversation-contract'

const CHAT_ENDPOINT = '/api/chat'

type SendChatMessageInput = {
  character: string
  conversationId: string
  message: string
}

type ChatResponse = {
  character: string
  response: string
}

const isRecord = (value: unknown): value is Record<string, unknown> => {
  return typeof value === 'object' && value !== null
}

const parseChatResponse = async (response: Response, expectedCharacter: string): Promise<ChatResponse> => {
  if (!response.ok) {
    throw new Error(`Chat request failed with status ${response.status}`)
  }

  const body: unknown = await response.json()
  if (!isRecord(body) || typeof body.character !== 'string' || typeof body.response !== 'string') {
    throw new Error('Chat response shape is invalid')
  }
  if (body.character !== expectedCharacter) {
    throw new Error('Chat response character does not match the request')
  }

  return { character: body.character, response: body.response }
}

export const sendChatMessage = async (input: SendChatMessageInput): Promise<ChatResponse> => {
  const response = await fetch(CHAT_ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      character: input.character,
      [CONVERSATION_ID_FIELD]: input.conversationId,
      message: input.message,
    }),
  })

  return parseChatResponse(response, input.character)
}
