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

  test.each([
    { protocol_version: '0.9', type: 'state_sync_request', generation: 1 },
    { protocol_version: '1.0', type: 'unknown_frame', generation: 1 },
  ])('protocol不一致と未知typeを拒否する', (frame) => {
    expect(() => parsePrivateFrame(frame)).toThrow(
      'LiveKit private frame does not match protocol 1.0',
    )
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


test('応答track準備通知は応答IDとSIDを検証して正規化する', () => {
  const frame = { protocol_version: '1.0', type: 'response_track_ready', generation: 2,
    response_id: '50000000-0000-4000-8000-000000000001', track_sid: 'TR_one' }
  expect(parsePrivateFrame(frame)).toEqual({type: 'response_track_ready', generation: 2,
    responseId: frame.response_id, trackSid: 'TR_one'})
  for (const invalid of [{...frame, response_id: 'invalid'}, {...frame, track_sid: 'other'},
    {...frame, generation: -1}, {...frame, unexpected: true}]) {
    expect(() => parsePrivateFrame(invalid)).toThrow()
  }
})


test('送信完了の総sample数を検証し、PCM本文や矛盾した総数を許可しない', () => {
  const frame = {protocol_version: '1.0', type: 'response_audio_finished',
    response_id: '50000000-0000-4000-8000-000000000001', generation: 0,
    input_sample_count: 1234, captured_sample_count: 2880, padding_sample_count: 1646}
  expect(parsePrivateFrame(frame)).toMatchObject({type: 'response_audio_finished', inputSampleCount: 1234, capturedSampleCount: 2880})
  expect(() => parsePrivateFrame({...frame, captured_sample_count: 1920})).toThrow()
  expect(() => parsePrivateFrame({...frame, pcm: 'body'})).toThrow()
  expect(() => parsePrivateFrame({...frame, input_sample_count: -1})).toThrow()
})

const controlProbe = {protocol_version: '1.0', type: 'control_probe',
  generation: 2, probe_id: '10000000-0000-4000-8000-000000000001'}
test.each(['control_probe', 'control_probe_ack'])('制御probeはnonceと世代を保持する: %s', type => {
  expect(parsePrivateFrame({...controlProbe, type})).toEqual({type, generation: 2, probeId: controlProbe.probe_id})
})
test.each([
  {...controlProbe, probe_id: 'invalid'}, {...controlProbe, generation: -1},
  {...controlProbe, body: 'private'}, {...controlProbe, probe_id: undefined},
])('不正なprobeや本文の混入を拒否する', frame => {
  expect(() => parsePrivateFrame(frame)).toThrow()
})

test('音声probeは固定長の診断音とnonce・世代・trackだけを通知する', () => {
  const base = {protocol_version: '1.0', generation: 1, probe_id: controlProbe.probe_id}
  expect(parsePrivateFrame({...base, type: 'audio_probe_request'})).toEqual({
    type: 'audio_probe_request', generation: 1, probeId: base.probe_id})
  for (const type of ['audio_probe_ready', 'audio_probe_complete']) {
    expect(parsePrivateFrame({...base, type, track_sid: 'TR_probe'})).toEqual({
      type, generation: 1, probeId: base.probe_id, trackSid: 'TR_probe'})
  }
  const finished = {...base, type: 'audio_probe_finished', track_sid: 'TR_probe',
    input_sample_count: 9600, captured_sample_count: 10560, padding_sample_count: 960}
  expect(parsePrivateFrame(finished)).toEqual({type: 'audio_probe_finished', generation: 1,
    probeId: base.probe_id, trackSid: 'TR_probe', inputSampleCount: 9600, capturedSampleCount: 10560, paddingSampleCount: 960})
  for (const invalid of [{...finished, input_sample_count: 48000}, {...finished, captured_sample_count: 9600},
    {...finished, padding_sample_count: 0}, {...finished, track_sid: ''}, {...finished, track_sid: 'other'},
    {...finished, pcm: 'private'}, {...finished, generation: -1}, {...finished, probe_id: 'invalid'}]) {
    expect(() => parsePrivateFrame(invalid)).toThrow()
  }
})


test('時計要求と対になったserver時計を検証し、片方のみ・逆行・unsafe integerを拒否する', () => {
  const common = {protocol_version: '1.0', probe_id: '10000000-0000-4000-8000-000000000010', generation: 0}
  expect(parsePrivateFrame({...common, type: 'control_probe', observe_clock: true})).toMatchObject({observeClock: true})
  const frame = {...common, type: 'control_probe_ack', server_received_us: 1000, server_sent_us: 1005}
  expect(parsePrivateFrame(frame)).toMatchObject({serverReceivedAtUs: 1000, serverSentAtUs: 1005})
  for (const changed of [{server_sent_us: undefined}, {server_received_us: undefined},
    {server_sent_us: 999}, {server_sent_us: Number.MAX_SAFE_INTEGER + 1}, {server_received_us: true}]) {
    expect(() => parsePrivateFrame({...frame, ...changed})).toThrow()
  }
})
