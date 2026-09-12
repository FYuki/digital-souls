import type {VoiceSessionEvent} from '../lib/voice-session/generated'
import type {VoiceSessionContext} from './voice-session'

export type TextSubmissionStatus = 'sending' | 'confirming' | 'accepted' | 'rejected' | 'not_received'

export type TextSubmission = Readonly<{
  inputId: string
  sessionId: string
  context: VoiceSessionContext
  text: string
  status: TextSubmissionStatus
  responseId: string | null
  errorCode: string | null
}>

export const submissionPending = (submission: TextSubmission): boolean =>
  submission.status === 'sending' || submission.status === 'confirming'

const sameThread = (left: VoiceSessionContext, right: VoiceSessionContext): boolean =>
  left.characterId === right.characterId && left.conversationId === right.conversationId

/** 配送ACKと受理結果を分ける、画面・DOMに依存しない送信状態。 */
export class TextSubmissionTracker {
  private readonly entries = new Map<string, TextSubmission>()

  snapshot(): readonly TextSubmission[] {
    return [...this.entries.values()]
  }

  hasPending(context: VoiceSessionContext): boolean {
    return this.snapshot().some(entry => sameThread(entry.context, context) && submissionPending(entry))
  }

  pending(sessionId: string): readonly TextSubmission[] {
    return this.snapshot().filter(entry => entry.sessionId === sessionId && submissionPending(entry))
  }

  begin(event: VoiceSessionEvent, context: VoiceSessionContext): void {
    if (event.type !== 'user_text_submitted' || event.text === undefined) {
      throw new Error('text submission event is required')
    }
    if (this.entries.has(event.event_id) || this.hasPending(context)) {
      throw new Error('previous text submission is unresolved')
    }
    if (this.entries.size >= 256) throw new Error('text submission capacity exceeded')
    this.entries.set(event.event_id, Object.freeze({
      inputId: event.event_id, sessionId: event.session_id, context: Object.freeze({...context}),
      text: event.text, status: 'sending', responseId: null, errorCode: null,
    }))
  }

  markUnknown(sessionId: string, inputId?: string): void {
    for (const entry of this.pending(sessionId)) {
      if (inputId !== undefined && entry.inputId !== inputId) continue
      this.entries.set(entry.inputId, Object.freeze({...entry, status: 'confirming'}))
    }
  }

  receive(event: VoiceSessionEvent): boolean {
    if (event.type !== 'user_input_result' || event.input_event_id === undefined) return false
    const entry = this.entries.get(event.input_event_id)
    if (entry === undefined || entry.sessionId !== event.session_id) return false
    // 再送・照合の遅着processingで確定済み結果を巻き戻さない。
    if (!submissionPending(entry)) return true
    if (event.status === 'processing') return true
    if (event.status !== 'accepted' && event.status !== 'rejected' && event.status !== 'not_received') return false
    this.entries.set(entry.inputId, Object.freeze({
      ...entry, status: event.status, responseId: event.response_id ?? null,
      errorCode: event.error_code ?? null,
    }))
    return true
  }

  releaseResolvedSessions(activeSessionId: string | null): void {
    for (const entry of this.entries.values()) {
      if (entry.sessionId !== activeSessionId && !submissionPending(entry)) this.entries.delete(entry.inputId)
    }
  }
}
