import {describe, expect, test} from 'vitest'
import {readBackendVadObservation, type BackendVadInput, type CoreDiagnostic} from '../playwright/backend-vad-diagnostic'

const event = (type: string, utteranceId: string, atMs: number, startSample: number, detectedSample: number): CoreDiagnostic => ({
  type, utteranceId, atMs, sessionId: 'session', trackSid: 'TR_microphone', inputGeneration: 1,
  startSample, detectedSample, activeEndSample: detectedSample - 100,
  sampleRate: 16000, clockDomain: 'server_monotonic', serverTimestampMs: atMs + 100000,
})
function input(): BackendVadInput {
  return {overflow: false, sessionId: 'session', initialUtteranceId: 'initial', sourceStartLowerMs: 1000,
    events: [
      event('speech_started', 'initial', 10, 0, 1000),
      event('speech_started', 'pause', 1300, 10000, 14000),
      event('speech_stopped', 'pause', 2600, 10000, 34800),
      {type: 'utterance_finalized', utteranceId: 'pause', sessionId: 'session', atMs: 2900},
    ]}
}

describe('BEのpause診断', () => {
  test('FE VADなしで正式境界と最終STTを照合する', () => {
    expect(readBackendVadObservation(input())).toEqual({
      status: 'observed', reason: null, confirmed: 1, ended: 1, finalized: 1, utteranceIds: ['pause'],
    })
  })
  test('2つに分割された発話を1つへまとめない', () => {
    const value = input()
    value.events = [...value.events,
      event('speech_started', 'second', 3000, 35000, 40000),
      event('speech_stopped', 'second', 4200, 35000, 55000),
      {type: 'utterance_finalized', utteranceId: 'second', sessionId: 'session', atMs: 4400}]
    expect(readBackendVadObservation(value)).toMatchObject({status: 'observed', confirmed: 2, ended: 2, finalized: 2})
  })
  test.each([
    ['generation', 'backend_input_identity_mismatch'],
    ['session', 'backend_input_identity_mismatch'],
    ['samples', 'backend_input_identity_mismatch'],
    ['order', 'backend_boundary_order_invalid'],
    ['duplicate', 'backend_boundary_duplicated'],
    ['unpaired', 'backend_boundary_unpaired'],
    ['end', 'speech_end_unavailable'],
    ['final', 'final_utterance_unavailable'],
    ['discard', 'backend_input_discarded'],
    ['overflow', 'backend_event_overflow'],
  ])('%s の不完全な証跡を成功にしない', (damage, reason) => {
    const value = input()
    const events = [...value.events]
    if (damage === 'generation') events[2].inputGeneration = 2
    if (damage === 'session') events[1].sessionId = 'another'
    if (damage === 'samples') events[1].startSample = -1
    if (damage === 'order') events[2].startSample = 11000
    if (damage === 'duplicate') events.push({...events[1]})
    if (damage === 'unpaired') events.push({...events[2], utteranceId: 'another'})
    if (damage === 'end') events.splice(2, 1)
    if (damage === 'final') events.pop()
    if (damage === 'discard') events.push({type: 'utterance_discarded', utteranceId: 'pause', atMs: 2800})
    if (damage === 'overflow') value.overflow = true
    value.events = events
    expect(readBackendVadObservation(value)).toMatchObject({status: 'missing', reason})
  })
  test('通知の順序が前後してもBE時計とsampleの因果順序で照合する', () => {
    const value = input()
    value.events = [value.events[0], value.events[2], value.events[3], value.events[1]]
    expect(readBackendVadObservation(value).status).toBe('observed')
  })
})
