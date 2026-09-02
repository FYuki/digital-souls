import type { Conversation, ConversationTurn } from './types'
import { parsePersistedTurn } from './turn-parser'

const API_PREFIX = '/api/characters'
const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === 'object' && value !== null
)

const requestJson = async (url: string, init?: RequestInit): Promise<unknown> => {
  const response = await fetch(url, init)
  if (!response.ok) throw new Error(`Conversation request failed with status ${response.status}`)
  return response.status === 204 ? null : response.json()
}

const requestWithoutBody = async (url: string, init: RequestInit): Promise<void> => {
  const response = await fetch(url, init)
  if (!response.ok) throw new Error(`Conversation request failed with status ${response.status}`)
}

const basePath = (character: string): string => (
  `${API_PREFIX}/${encodeURIComponent(character)}/conversations`
)

const parseConversation = (value: unknown): Conversation => {
  if (
    !isRecord(value)
    || typeof value.character_id !== 'string'
    || typeof value.conversation_id !== 'string'
    || !UUID_V4_PATTERN.test(value.conversation_id)
    || typeof value.created_at !== 'string'
    || typeof value.updated_at !== 'string'
    || !(typeof value.archived_at === 'string' || value.archived_at === null)
    || typeof value.title !== 'string'
    || value.title.length < 1
    || value.title.length > 40
  ) throw new Error('Conversation response shape is invalid')
  return value as Conversation
}

const parseConversationList = (
  value: unknown,
  character: string,
  archived: boolean,
): Conversation[] => {
  if (!Array.isArray(value)) throw new Error('Conversation list response shape is invalid')
  const conversations = value.map(parseConversation)
  if (conversations.some((item) => (
    item.character_id !== character || (item.archived_at !== null) !== archived
  ))) throw new Error('Conversation list response boundary is invalid')
  return conversations
}

export const listActiveConversations = async (character: string): Promise<Conversation[]> => (
  parseConversationList(await requestJson(basePath(character)), character, false)
)

export const listArchivedConversations = async (character: string): Promise<Conversation[]> => (
  parseConversationList(
    await requestJson(`${basePath(character)}/archived`),
    character,
    true,
  )
)

export const createConversation = async (character: string): Promise<Conversation> => {
  const conversation = parseConversation(
    await requestJson(basePath(character), { method: 'POST' }),
  )
  if (conversation.character_id !== character || conversation.archived_at !== null) {
    throw new Error('Created conversation response boundary is invalid')
  }
  return conversation
}

export const listConversationTurns = async (
  character: string,
  conversationId: string,
): Promise<ConversationTurn[]> => {
  const value = await requestJson(`${basePath(character)}/${conversationId}/turns`)
  if (!Array.isArray(value)) throw new Error('Conversation history response shape is invalid')
  return value.map(parsePersistedTurn)
}

const transition = async (
  character: string,
  conversationId: string,
  operation: 'archive' | 'unarchive',
): Promise<Conversation> => {
  const conversation = parseConversation(await requestJson(
    `${basePath(character)}/${conversationId}/${operation}`,
    { method: 'POST' },
  ))
  const shouldBeArchived = operation === 'archive'
  if (
    conversation.character_id !== character
    || conversation.conversation_id !== conversationId
    || (conversation.archived_at !== null) !== shouldBeArchived
  ) throw new Error('Conversation transition response boundary is invalid')
  return conversation
}

export const archiveConversation = async (character: string, conversationId: string) => (
  transition(character, conversationId, 'archive')
)

export const unarchiveConversation = async (character: string, conversationId: string) => (
  transition(character, conversationId, 'unarchive')
)

export const renameConversation = async (
  character: string,
  conversationId: string,
  title: string,
): Promise<Conversation> => {
  const conversation = parseConversation(await requestJson(
    `${basePath(character)}/${conversationId}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    },
  ))
  if (
    conversation.character_id !== character
    || conversation.conversation_id !== conversationId
  ) throw new Error('Renamed conversation response boundary is invalid')
  return conversation
}

export const hardDeleteConversation = async (
  character: string,
  conversationId: string,
): Promise<void> => {
  await requestWithoutBody(`${basePath(character)}/${conversationId}`, { method: 'DELETE' })
}
