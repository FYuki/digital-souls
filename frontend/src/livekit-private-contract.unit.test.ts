import { describe, expect, test } from 'vitest'

import { parsePrivateFrame } from './livekit/private-contract'

const authoritativeState = (terminalOutcomes: unknown[]): Record<string, unknown> => ({
  protocol_version: '1.0',
  type: 'authoritative_state',
  generation: 1,
  session_phase: 'available',
  terminal_outcomes: terminalOutcomes,
})

const terminalOutcome = {
  type: 'response_interrupted',
  session_id: '20000000-0000-4000-8000-000000000010',
  response_id: '30000000-0000-4000-8000-000000000010',
  confirmed_audio_sequence: 0,
}

describe('LiveKit private contract', () => {
  test.each([
    {
      protocol_version: '1.0',
      type: 'ack',
      event_id: '10000000-0000-4000-8000-000000000010',
      generation: 0,
    },
    {
      protocol_version: '1.0',
      type: 'state_sync_request',
      generation: 1,
    },
    authoritativeState([]),
    {
      protocol_version: '1.0',
      type: 'logical_audio_segment',
      response_id: '30000000-0000-4000-8000-000000000010',
      audio_sequence: 0,
      generation: 1,
      pcm_sample_count: 480,
    },
    {
      protocol_version: '1.0',
      type: 'microphone_observation',
      generation: 1,
      frame_count: 1,
      sample_count: 480,
      elapsed_ms: 0,
      missing_frames: 0,
    },
  ])('共有schemaに適合するprivate frameを受理する', (frame) => {
    expect(() => parsePrivateFrame(frame)).not.toThrow()
  })

  test('authoritative stateのterminal outcomeをconsumer形式へ正規化する', () => {
    expect(parsePrivateFrame(authoritativeState([terminalOutcome]))).toEqual({
      type: 'authoritative_state',
      generation: 1,
      sessionPhase: 'available',
      terminalOutcomes: [{
        type: 'response_interrupted',
        sessionId: terminalOutcome.session_id,
        responseId: terminalOutcome.response_id,
        confirmedAudioSequence: 0,
      }],
    })
  })

  test.each([
    {},
    {
      type: 'response_interrupted',
      session_id: terminalOutcome.session_id,
      response_id: terminalOutcome.response_id,
    },
    { ...terminalOutcome, session_id: 'not-a-uuid' },
    { ...terminalOutcome, confirmed_audio_sequence: -1 },
    { ...terminalOutcome, unexpected: true },
  ])('共有schemaに適合しないterminal outcomeを拒否する', (outcome) => {
    expect(() => parsePrivateFrame(authoritativeState([outcome]))).toThrow(
      'LiveKit private frame does not match protocol 1.0',
    )
  })
})
