import {describe, expect, test} from 'vitest'
import {parseVoiceSessionEvent} from './lib/voice-session/validation'
import {VoiceHistoryProjection} from './livekit/history-projection'
import {InputSuppressionPolicy} from './livekit/input-suppression'
import {TextSubmissionTracker} from './livekit/text-input'

const sessionId = '20000000-0000-4000-8000-000000000001'
const inputId = '10000000-0000-4000-8000-000000000001'
const speechId = '30000000-0000-4000-8000-000000000001'
const responseId = '50000000-0000-4000-8000-000000000001'
const context = {characterId: 'miori', conversationId: 'thread-a'}
const event = (fields: Record<string, unknown>) => parseVoiceSessionEvent({
  protocol_version: '1.1', event_id: crypto.randomUUID(), session_id: sessionId,
  monotonic_timestamp_ms: 1, ...fields,
})

describe('DOM非依存のConversation client共通部品', () => {
  test('共有protocolからSpeech/Textの順序・発生元・履歴表示を投影する', () => {
    const submissions = new TextSubmissionTracker()
    const projection = new VoiceHistoryProjection()
    submissions.begin(event({type: 'user_text_submitted', event_id: inputId, text: '直接の質問',
      speaker: {role: 'user', participant_id: inputId}}), context)
    projection.receive(event({type: 'utterance_finalized', utterance_id: speechId,
      transcript: '音声で補足', should_response: false,
      speaker: {role: 'user', participant_id: inputId}}), context, [])
    const start = event({type: 'response_started', response_id: responseId,
      source_utterance_ids: [speechId], source_inputs: [
        {input_id: speechId, source: 'speech'}, {input_id: inputId, source: 'text'},
      ], speaker: {role: 'character', participant_id: inputId, character_id: 'miori'}})
    expect(projection.receive(start, context, submissions.snapshot())).toMatchObject({
      context, userContent: '音声で補足\n直接の質問', status: 'generating',
    })
    expect(projection.receive(start, context, submissions.snapshot())).toBeNull()
    const delta = event({type: 'response_delta', response_id: responseId, text_sequence: 1,
      text: '回答', text_range: {start: 0, end: 2}})
    expect(projection.receive(delta, {...context, conversationId: 'thread-b'}, [])).toBeNull()
    expect(projection.receive(delta, context, [])).toMatchObject({context, assistantContent: '回答'})
    expect(projection.receive(delta, context, [])).toBeNull()
    const completed = event({type: 'response_completed', response_id: responseId,
      last_text_sequence: 1, last_audio_sequence: 0})
    expect(projection.receive(completed, context, [])).toMatchObject({status: 'completed', assistantContent: '回答'})
    expect(projection.receive(delta, context, [])).toBeNull()
  })

  test('focus解除・送信成功相当のblurでは持続muteを解除しない', () => {
    const policy = new InputSuppressionPolicy()
    policy.resumeExplicitly()
    expect(policy.suppressed).toBe(false)
    policy.setFocused(true)
    policy.mute()
    policy.setFocused(false)
    expect(policy.snapshot()).toEqual(['manual'])
    policy.resumeExplicitly()
    policy.switchThread()
    policy.setFocused(true)
    policy.setFocused(false)
    expect(policy.snapshot()).toEqual(['thread_switch'])
    policy.resumeExplicitly()
    expect(policy.suppressed).toBe(false)
  })
})
