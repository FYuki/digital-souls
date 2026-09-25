import { get } from 'svelte/store'
import { vi } from 'vitest'

import type { VoiceSessionContext } from '../../livekit/voice-session'
import type { SelectedConversationContext } from '../conversations/controller'
import type { ConversationTurn } from '../conversations/types'
import type { VoiceSessionEvent } from '../voice-session/generated'
import { parseVoiceSessionEvent } from '../voice-session/validation'
import { createVoiceHistoryController, visibleVoiceTurns, type VoiceHistoryController } from './controller'


export const SESSION_ID = '20000000-0000-4000-8000-000000000001'
export const PARTICIPANT_ID = '40000000-0000-4000-8000-000000000001'
export const UTTERANCE_ID = '30000000-0000-4000-8000-000000000001'
export const SECOND_UTTERANCE_ID = '30000000-0000-4000-8000-000000000002'
export const TEXT_INPUT_ID = '60000000-0000-4000-8000-000000000001'
export const RESPONSE_ID = '50000000-0000-4000-8000-000000000001'
export const SECOND_RESPONSE_ID = '50000000-0000-4000-8000-000000000002'
export const UNRELATED_RESPONSE_ID = '50000000-0000-4000-8000-000000000099'
export const HISTORY_TURN_ID = '9e70795d-e5d5-431d-baa2-67f884403010'
export const SECOND_HISTORY_TURN_ID = '9e70795d-e5d5-431d-baa2-67f884403020'

export const contextA: VoiceSessionContext = { characterId: 'miori', conversationId: 'conversation-a' }
export const contextAkari: VoiceSessionContext = { characterId: 'akari', conversationId: 'conversation-c' }

export const viewA = { character: 'miori', conversationId: 'conversation-a' }
export const viewB = { character: 'miori', conversationId: 'conversation-b' }
export const viewAkari = { character: 'akari', conversationId: 'conversation-c' }

export const selectionA = (version = 1): SelectedConversationContext => ({
  character: 'miori',
  conversationId: 'conversation-a',
  version,
})
export const selectionB = (version = 2): SelectedConversationContext => ({
  character: 'miori',
  conversationId: 'conversation-b',
  version,
})

let eventSequence = 0
export const voiceEvent = (fields: Record<string, unknown>): VoiceSessionEvent => parseVoiceSessionEvent({
  protocol_version: '2.0',
  event_id: `10000000-0000-4000-8000-${String(++eventSequence).padStart(12, '0')}`,
  session_id: SESSION_ID,
  monotonic_timestamp_ms: 1_000,
  ...fields,
})

export const utteranceFinalized = (
  utteranceId: string,
  transcript: string,
  shouldResponse = true,
): VoiceSessionEvent => voiceEvent({
  type: 'utterance_finalized',
  utterance_id: utteranceId,
  transcript,
  should_response: shouldResponse,
  speaker: { participant_id: PARTICIPANT_ID, role: 'user' },
})

export const responseStarted = (
  responseId: string,
  options: {
    historyTurnId?: string
    speechSources?: string[]
    sourceInputs?: { input_id: string; source: 'speech' | 'text' }[]
  } = {},
): VoiceSessionEvent => voiceEvent({
  type: 'response_started',
  response_id: responseId,
  speaker: { participant_id: PARTICIPANT_ID, role: 'character', character_id: 'miori' },
  source_utterance_ids: options.speechSources ?? [UTTERANCE_ID],
  ...(options.historyTurnId === undefined ? {} : { history_turn_id: options.historyTurnId }),
  ...(options.sourceInputs === undefined ? {} : { source_inputs: options.sourceInputs }),
})

export const responseDelta = (
  responseId: string,
  sequence: number,
  text: string,
): VoiceSessionEvent => voiceEvent({
  type: 'response_delta',
  response_id: responseId,
  text_sequence: sequence,
  text,
  text_range: { start: 0, end: text.length },
})

export const responseTerminal = (
  type: 'response_completed' | 'response_cancelled' | 'response_failed',
  responseId: string,
): VoiceSessionEvent => {
  if (type === 'response_completed') {
    return voiceEvent({ type, response_id: responseId, last_text_sequence: 1, last_audio_sequence: 0 })
  }
  if (type === 'response_cancelled') {
    return voiceEvent({ type, response_id: responseId, reason: 'barge_in' })
  }
  return voiceEvent({
    type, response_id: responseId, error_code: 'streaming_pipeline_failed', recoverable: false,
  })
}

export const privacySkipped = (
  responseId: string,
  sourceInputs: { input_id: string; source: 'speech' | 'text' }[],
): VoiceSessionEvent => voiceEvent({
  type: 'response_privacy_skipped',
  response_id: responseId,
  source_inputs: sourceInputs,
})

export const utteranceDiscarded = (utteranceId: string): VoiceSessionEvent => voiceEvent({
  type: 'utterance_discarded',
  utterance_id: utteranceId,
  reason: 'privacy',
})

export const deferred = <T,>() => {
  let resolve: (value: T | PromiseLike<T>) => void = () => {
    throw new Error('resolver is not initialized')
  }
  const promise = new Promise<T>((settle) => {
    resolve = settle
  })
  return { promise, resolve }
}

export const flush = () => new Promise<void>((resolve) => {
  setTimeout(resolve, 0)
})

export const savedTurn = (turnId: string): ConversationTurn => ({
  kind: 'content',
  turn_id: turnId,
  user_content: '保存済みの質問',
  assistant_content: '保存済みの回答',
})

export const createHarness = () => {
  let selection: SelectedConversationContext | null = null
  let saved: ConversationTurn[] = []
  const refreshTurns = vi.fn(async (_context: SelectedConversationContext) => {})
  const refreshCharacter = vi.fn(async (_characterId: string) => {})
  const controller = createVoiceHistoryController({
    selectedContext: () => selection,
    refreshTurns,
    savedTurns: () => saved,
    refreshCharacter,
  })
  return {
    controller,
    refreshTurns,
    refreshCharacter,
    select: (next: SelectedConversationContext | null) => {
      selection = next
    },
    saveTurns: (turns: ConversationTurn[]) => {
      saved = turns
    },
  }
}

export const view = (
  controller: VoiceHistoryController,
  selected: { character: string; conversationId: string } | null,
) => visibleVoiceTurns(get(controller), selected)
