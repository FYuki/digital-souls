export type Conversation = {
  character_id: string
  conversation_id: string
  created_at: string
  updated_at: string
  archived_at: string | null
  title: string
}

export type ContentTurn = {
  kind: 'content'
  turn_id: string
  user_content: string
  assistant_content: string
}

export type PrivacySkippedTurn = {
  kind: 'privacy_skipped'
  turn_id: string
  reason_code: string
  sanitizer_version: string
  policy_version: string
}

export type ConversationTurn = ContentTurn | PrivacySkippedTurn
