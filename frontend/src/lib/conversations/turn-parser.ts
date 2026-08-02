import type { ContentTurn, ConversationTurn, PrivacySkippedTurn } from './types'

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === 'object' && value !== null
)

export const parsePersistedTurn = (value: unknown): ConversationTurn => {
  if (!isRecord(value) || typeof value.turn_id !== 'string') {
    throw new Error('Conversation turn response shape is invalid')
  }
  if (
    value.kind === 'content'
    && typeof value.user_content === 'string'
    && typeof value.assistant_content === 'string'
  ) return {
    kind: 'content',
    turn_id: value.turn_id,
    user_content: value.user_content,
    assistant_content: value.assistant_content,
  } as ContentTurn
  if (
    value.kind === 'privacy_skipped'
    && typeof value.reason_code === 'string'
    && typeof value.sanitizer_version === 'string'
    && typeof value.policy_version === 'string'
    && !('user_content' in value)
    && !('assistant_content' in value)
  ) return {
    kind: 'privacy_skipped',
    turn_id: value.turn_id,
    reason_code: value.reason_code,
    sanitizer_version: value.sanitizer_version,
    policy_version: value.policy_version,
  } as PrivacySkippedTurn
  throw new Error('Conversation turn response shape is invalid')
}
