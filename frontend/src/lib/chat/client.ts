import { CONVERSATION_ID_FIELD } from '../conversation-contract'
import type { ConversationTurn } from '../conversations/types'
import { parsePersistedTurn } from '../conversations/turn-parser'
import type { SnapshotRequested } from '../screen-perception/generated'
import { parseScreenPerceptionEvent } from '../screen-perception/validation'

const CHAT_ENDPOINT = '/api/chat'

type SendChatMessageInput = {
  character: string
  conversationId: string
  message: string
  screenReference?: boolean
  screenClientSessionId?: string | null
}

export type ChatResponse = {
  character: string
  turn: ConversationTurn
}

export type ChatRequestResult =
  | { kind: 'completed'; chat: ChatResponse }
  | { kind: 'snapshot_requested'; request: SnapshotRequested }

const isRecord = (value: unknown): value is Record<string, unknown> => {
  return typeof value === 'object' && value !== null
}

export const parseChatResponseBody = (
  body: unknown,
  expectedCharacter: string,
): ChatResponse => {
  if (!isRecord(body) || typeof body.character !== 'string' || !('turn' in body)) {
    throw new Error('Chat response shape is invalid')
  }
  if (body.character !== expectedCharacter) {
    throw new Error('Chat response character does not match the request')
  }
  return { character: body.character, turn: parsePersistedTurn(body.turn) }
}

const parseChatResponse = async (response: Response, expectedCharacter: string): Promise<ChatResponse> => {
  if (!response.ok) {
    throw new Error(`Chat request failed with status ${response.status}`)
  }

  const body: unknown = await response.json()
  return parseChatResponseBody(body, expectedCharacter)
}

export const sendChatRequest = async (input: SendChatMessageInput): Promise<ChatRequestResult> => {
  const response = await fetch(CHAT_ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      character: input.character,
      [CONVERSATION_ID_FIELD]: input.conversationId,
      message: input.message,
      ...(input.screenReference === true ? { screen_reference: true } : {}),
      ...(input.screenClientSessionId === undefined || input.screenClientSessionId === null
        ? {}
        : { screen_client_session_id: input.screenClientSessionId }),
    }),
  })
  if (response.status === 202) {
    const event = parseScreenPerceptionEvent(await response.json())
    if (event.type !== 'screen_snapshot_requested') {
      throw new Error('Chat snapshot response shape is invalid')
    }
    return { kind: 'snapshot_requested', request: event }
  }
  return { kind: 'completed', chat: await parseChatResponse(response, input.character) }
}

export const sendChatMessage = async (input: SendChatMessageInput): Promise<ChatResponse> => {
  const result = await sendChatRequest(input)
  if (result.kind !== 'completed') {
    throw new Error('Screen snapshot is required for this chat request')
  }
  return result.chat
}
