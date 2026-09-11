import type {VoiceSessionEvent} from '../lib/voice-session/generated'
import type {TextSubmission} from './text-input'
import type {VoiceSessionContext} from './voice-session'

export type ProjectedVoiceTurn = Readonly<{
  sessionId: string
  context: VoiceSessionContext
  responseId: string
  historyTurnId: string | null
  userContent: string
  assistantContent: string
  lastTextSequence: number
  status: 'generating' | 'completed' | 'cancelled' | 'failed' | 'privacy_skipped'
}>

/** 履歴の正本はBE。これは確定イベントから作る表示用の一時投影。 */
export class VoiceHistoryProjection {
  private readonly speech = new Map<string, string>()
  private readonly turns = new Map<string, ProjectedVoiceTurn>()

  receive(event: VoiceSessionEvent, context: VoiceSessionContext,
    submissions: readonly TextSubmission[]): ProjectedVoiceTurn | null {
    if (event.type === 'utterance_finalized' && event.utterance_id !== undefined) {
      this.speech.set(`${event.session_id}:${event.utterance_id}`, event.transcript ?? '')
      return null
    }
    if (event.response_id === undefined) return null
    const key = `${event.session_id}:${event.response_id}`
    const previous = this.turns.get(key)
    if (previous !== undefined && (previous.context.characterId !== context.characterId
      || previous.context.conversationId !== context.conversationId)) return null
    if (event.type === 'response_started') {
      if (previous !== undefined) return null
      const sources = event.source_inputs ?? (event.source_utterance_ids ?? [])
        .map(input_id => ({input_id, source: 'speech' as const}))
      const text = sources.map(source => source.source === 'text'
        ? submissions.find(entry => entry.sessionId === event.session_id && entry.inputId === source.input_id
          && entry.context.characterId === context.characterId
          && entry.context.conversationId === context.conversationId)?.text ?? ''
        : this.speech.get(`${event.session_id}:${source.input_id}`) ?? '')
      const turn: ProjectedVoiceTurn = Object.freeze({
        sessionId: event.session_id, context: Object.freeze({...context}), responseId: event.response_id,
        historyTurnId: event.history_turn_id ?? null, userContent: text.filter(Boolean).join('\n'),
        assistantContent: '', lastTextSequence: 0, status: 'generating',
      })
      this.turns.set(key, turn)
      for (const source of sources) {
        if (source.source === 'speech') this.speech.delete(`${event.session_id}:${source.input_id}`)
      }
      return turn
    }
    if (previous === undefined || previous.status !== 'generating') return null
    if (event.type === 'response_delta' && event.text_sequence === previous.lastTextSequence + 1
      && event.text !== undefined) {
      const turn = Object.freeze({...previous, assistantContent: previous.assistantContent + event.text,
        lastTextSequence: event.text_sequence})
      this.turns.set(key, turn)
      return turn
    }
    const terminal = {
      response_completed: 'completed', response_cancelled: 'cancelled',
      response_failed: 'failed', response_privacy_skipped: 'privacy_skipped',
    } as const
    if (!(event.type in terminal)) return null
    const turn = Object.freeze({...previous, status: terminal[event.type as keyof typeof terminal]})
    this.turns.set(key, turn)
    return turn
  }

  releaseSession(sessionId: string): void {
    for (const key of this.speech.keys()) if (key.startsWith(`${sessionId}:`)) this.speech.delete(key)
    for (const key of this.turns.keys()) if (key.startsWith(`${sessionId}:`)) this.turns.delete(key)
  }
}
