// @vitest-environment node
import {expect, test} from 'vitest'
import {summarizeFaultRecoveryCohort} from '../playwright/fault-recovery-cohort'
import {PacketOutputDiagnostic, type PacketOutputEvidence} from './livekit/packet-output-diagnostic'

const id = (i: number, kind: number) => `${kind}0000000-0000-4000-8000-${String(i).padStart(12, '0')}`
const samples = Array.from({length: 5}, (_, i) => ({sentAtMs: 100 + i, remoteAtMs: 100 + i, receivedAtMs: 100 + i}))
function trial(i = 1) {
  const rows: PacketOutputEvidence[] = []
  const diagnostic = new PacketOutputDiagnostic(id(i, 3), 'TR_private', 0, row => rows.push(row))
  diagnostic.receive({packetIndex: 0, rtpTimestamp: 99, source: 7, receivedAtMs: 3200, decodedAtMs: 3202,
    receivedAtBoundsMs: {lowerMs: 3200, upperMs: 3200.2}, decodedAtBoundsMs: {lowerMs: 3202, upperMs: 3202.2}, pcm: new Float32Array(960)})
  diagnostic.confirm({kind: 'rendered', packetIndex: 0, rtpTimestamp: 99, packetSampleOffset: 0,
    startFrame: 48000, endFrame: 48960, energy: 1, firstAudibleFrame: 48000}, 3212, 3235)
  return {measurement_revision: 'a'.repeat(40), measurement_scope: 'livekit_fault_recovery_session_diagnostic', fixture_sha256: 'a'.repeat(64),
    session_id: id(i, 1), initial_cycle: {conversationId: id(i, 2), sessionId: id(i, 1), responseId: id(i, 3)},
    session_end_confirmed: true, fault_clock_process_closed: true,
    post_fault_followup: {outcome: 'success', track_response_matches: true,
      cycle: {sessionId: id(i, 1), conversationId: id(i, 2), responseId: id(i, 4)},
      playback_completion: {expectedSamples: 960, renderedSamples: 960, gapSamples: 0, maximumGapSamples: 0}},
    fault_operation_succeeded: true, fault_duration_ms: 2000,
    fault_clock_before: {browser: samples, faultRunner: samples}, fault_clock_after: {browser: samples, faultRunner: samples},
    fault_events: ['network_link_disconnected', 'network_link_restore_started', 'network_link_restored', 'signaling_tcp_reachable']
      .map((event, index) => ({event, timestamp_ns: ['1000000000', '3000000000', '3090000000', '3100000000'][index]})),
    fault_probes: [{status: 'timeout', generation: 0, probeId: id(i, 5), sentAtMs: 1100, receivedAtMs: null, observedAtMs: 1600},
      {status: 'received', generation: 0, probeId: id(i, 6), sentAtMs: 3200, receivedAtMs: 3210, observedAtMs: 3210}],
    evidence: {packet_outputs: rows, packet_output_overflow: false, transport_failures: [] as Array<{stage: string}>}}
}

test('100独立試行をrawから再計算し、ID・raw・任意合否を匿名集計へ持ち込まない', () => {
  const records = Array.from({length: 100}, (_, i) => ({...trial(i), outcome: 'failure', recovery: {recovery_upper_ms: 999999}}))
  const report = summarizeFaultRecoveryCohort(records, 100)
  expect(report.counts.recovered_within_ten_seconds).toBe(100)
  expect(report.latency_ms.both.p95).toBeCloseTo(212.4)
  expect(report.evaluation.passed).toBe(true)
  expect(JSON.stringify(report)).not.toContain('TR_private')
  expect(JSON.stringify(report)).not.toContain(id(0, 1))
  expect(summarizeFaultRecoveryCohort([trial()], 1).evaluation.passed).toBe(false)
})

test('制御だけの復旧は失敗分母に残し、音声なしの全失敗に偽のp95を作らない', () => {
  const records = Array.from({length: 100}, (_, i) => trial(i))
  for (const row of records) {
    row.evidence.packet_outputs = row.evidence.packet_outputs.map(packet => packet.status === 'captured'
      ? {...packet, firstAudibleAtMs: undefined} : packet)
  }
  const report = summarizeFaultRecoveryCohort(records, 100)
  expect(report.counts.not_recovered).toBe(100)
  expect(report.latency_ms.both).toEqual({count: 0, p50: null, p95: null})
  expect(report.evaluation.coverage_complete).toBe(true)
  expect(report.evaluation.passed).toBe(false)
})

test.each(['clock', 'short_fault', 'no_effect', 'missing_output', 'output_failure', 'not_ended', 'followup_failed'])('欠測・終了失敗を合格で補完しない: %s', mode => {
  const record = trial()
  if (mode === 'clock') record.fault_clock_after = {browser: samples.map(row => ({...row, remoteAtMs: row.remoteAtMs + 10})), faultRunner: samples}
  if (mode === 'short_fault') record.fault_events[1].timestamp_ns = '2999999999'
  if (mode === 'no_effect') record.fault_probes.shift()
  if (mode === 'missing_output') record.evidence.packet_output_overflow = true
  if (mode === 'output_failure') record.evidence.transport_failures.push({stage: 'renderer'})
  if (mode === 'not_ended') record.session_end_confirmed = false
  if (mode === 'followup_failed') record.post_fault_followup.outcome = 'failure'
  const report = summarizeFaultRecoveryCohort([record], 1)
  expect(report.evaluation.passed).toBe(false)
  if (mode !== 'followup_failed') expect(report.evaluation.coverage_complete).toBe(false)
  else expect(report.counts.successful_followup_conversations).toBe(0)
})

test('重複session・conversation、別fixture、試行抜けを拒否する', () => {
  expect(() => summarizeFaultRecoveryCohort([trial()], 100)).toThrow('all expected')
  expect(() => summarizeFaultRecoveryCohort([trial(), trial()], 2)).toThrow('sessions')
  const second = trial(2); second.initial_cycle.conversationId = trial().initial_cycle.conversationId
  expect(() => summarizeFaultRecoveryCohort([trial(), second], 2)).toThrow('conversations')
  const another = trial(3); another.fixture_sha256 = 'b'.repeat(64)
  expect(() => summarizeFaultRecoveryCohort([trial(), another], 2)).toThrow('fixtures')
})


test('99/100の成功率と全成功試行のp95を別々に評価する', () => {
  const records = Array.from({length: 100}, (_, i) => trial(i))
  const silence = (i: number) => {records[i].evidence.packet_outputs = records[i].evidence.packet_outputs.map(row =>
    row.status === 'captured' ? {...row, firstAudibleAtMs: undefined} : row)}
  silence(99)
  expect(summarizeFaultRecoveryCohort(records, 100).evaluation.passed).toBe(true)
  silence(98)
  expect(summarizeFaultRecoveryCohort(records, 100).evaluation.rate_passed).toBe(false)
  const slow = Array.from({length: 100}, (_, i) => trial(i))
  for (const trial of slow) trial.evidence.packet_outputs = trial.evidence.packet_outputs.map(row => row.status === 'captured'
    ? {...row, outputAtMs: row.outputAtMs + 3000, confirmedAtMs: row.confirmedAtMs + 3000, firstAudibleAtMs: row.firstAudibleAtMs! + 3000} : row)
  const report = summarizeFaultRecoveryCohort(slow, 100)
  expect(report.counts.recovered_within_ten_seconds).toBe(100)
  expect(report.latency_ms.both.p95).toBeCloseTo(3212.4)
  expect(report.evaluation.rate_passed).toBe(true)
  expect(report.evaluation.latency_passed).toBe(false)
})

test('重複出力と復旧前に送信した要求の遅着ackを検出する', () => {
  const records = Array.from({length: 100}, (_, i) => trial(i))
  records[0].evidence.packet_outputs.push(records[0].evidence.packet_outputs[0])
  const report = summarizeFaultRecoveryCohort(records, 100)
  expect(report.duplicate_packet_output_intervals).toBe(1)
  expect(report.evaluation.passed).toBe(false)
  const late = trial()
  late.fault_probes[1].sentAtMs = 2900
  expect(summarizeFaultRecoveryCohort([late], 1).counts.recovered_within_ten_seconds).toBe(0)
})


test('異なる実装版を混ぜず、版情報がない既存rawは欠測として残す', () => {
  const different = trial(2); different.measurement_revision = 'b'.repeat(40)
  expect(() => summarizeFaultRecoveryCohort([trial(), different], 2)).toThrow('revisions')
  const legacy = {...trial(), measurement_revision: undefined}
  const report = summarizeFaultRecoveryCohort([legacy], 1)
  expect(report.counts.recovered_within_ten_seconds).toBe(1)
  expect(report.missing_reasons).toEqual({measurement_revision_unavailable: 1})
  expect(report.evaluation.coverage_complete).toBe(false)
})


test('匿名reportのschemaはIDや任意の欠測理由を拒否する', async () => {
  const {readFile} = await import('node:fs/promises')
  const {default: Ajv} = await import('ajv')
  const schema = JSON.parse(await readFile(new URL('../../docs/schemas/voice-quality-reconnect-report-v1.schema.json', import.meta.url), 'utf8'))
  const validate = new Ajv({strict: true}).compile(schema)
  const report = summarizeFaultRecoveryCohort([trial()], 1)
  expect(validate(report)).toBe(true)
  expect(validate({...report, session_id: id(1, 1)})).toBe(false)
  expect(validate({...report, missing_reasons: {'private-exception': 1}})).toBe(false)
})

function probeTrial() {
  const row = trial()
  const rows: PacketOutputEvidence[] = []
  const diagnostic = new PacketOutputDiagnostic(id(1, 7), 'TR_probe', 0, row => rows.push(row))
  for (let i = 0; i < 11; i++) {
    diagnostic.receive({packetIndex: i, rtpTimestamp: 99 + i * 960, source: 8,
      receivedAtMs: 3220 + i * 20, decodedAtMs: 3222 + i * 20,
      receivedAtBoundsMs: {lowerMs: 3220 + i * 20, upperMs: 3220.2 + i * 20},
      decodedAtBoundsMs: {lowerMs: 3222 + i * 20, upperMs: 3222.2 + i * 20}, pcm: new Float32Array(960)})
    diagnostic.confirm({kind: 'rendered', packetIndex: i, rtpTimestamp: 99 + i * 960, packetSampleOffset: 0,
      startFrame: 48000 + i * 960, endFrame: 48960 + i * 960, energy: 1, firstAudibleFrame: 48000 + i * 960},
    3300 + i * 20, 3325 + i * 20)
  }
  return {...row, audio_availability_method: 'fresh_rtc_probe_and_followup', audio_probe: {
    scope: 'rtc_audio_probe', status: 'captured', cleanupCompleted: true, probeId: id(1, 7), generation: 0,
    requestedAtMs: 3211, completedAtMs: 3600, trackSid: 'TR_probe', packetOutputs: rows,
    completion: {expectedSamples: 10560, inputSamples: 9600, paddingSamples: 960, renderedSamples: 10560,
      packetCount: 11, gapSamples: 0, maximumGapSamples: 0, gapCount: 0, sampleRate: 48000,
      firstOutputFrame: 48000, lastOutputEndFrame: 58560, firstRtpTimestamp: 99, lastRtpTimestamp: 9699}}}
}

test('新規RTC probeの全出力を再集計し、旧応答の時刻を復旧時間に使わない', () => {
  const report = summarizeFaultRecoveryCohort([probeTrial()], 1)
  expect(report.audio_availability_method).toBe('fresh_rtc_probe_and_followup')
  expect(report.evaluation.coverage_complete).toBe(true)
  expect(report.latency_ms.audio.p95).toBeCloseTo(300.4)
  expect(report.counts.recovered_within_ten_seconds).toBe(1)
  expect(() => summarizeFaultRecoveryCohort([probeTrial(), trial(2)], 2)).toThrow('audio availability methods')
})

test.each(['nonce', 'generation', 'track', 'pre_restore', 'before_control', 'packet_missing', 'packet_duplicate',
  'sample_count', 'source_changed', 'cleanup', 'status', 'silent', 'timestamp_invalid'])('probeの不一致・欠測を成功で補完しない: %s', mode => {
  const record = probeTrial(), probe = record.audio_probe
  if (mode === 'nonce') probe.probeId = id(2, 7)
  if (mode === 'generation') probe.generation++
  if (mode === 'track') probe.trackSid = 'TR_other'
  if (mode === 'pre_restore') probe.requestedAtMs = 3000
  if (mode === 'before_control') probe.requestedAtMs = 3209
  if (mode === 'packet_missing') probe.packetOutputs.pop()
  if (mode === 'packet_duplicate') probe.packetOutputs.push(probe.packetOutputs.at(-1)!)
  if (mode === 'sample_count') probe.completion.renderedSamples = 9600
  if (mode === 'source_changed') probe.packetOutputs = probe.packetOutputs.map((row, index) =>
    index === 1 && row.status === 'captured' ? {...row, packet: {...row.packet, source: 9}} : row)
  if (mode === 'cleanup') probe.cleanupCompleted = false
  if (mode === 'status') probe.status = 'failed'
  if (mode === 'silent') probe.packetOutputs = probe.packetOutputs.map(row => row.status === 'captured'
    ? {...row, firstAudibleAtMs: undefined, interval: {...row.interval, energy: 0}} : row)
  if (mode === 'timestamp_invalid') probe.packetOutputs = probe.packetOutputs.map(row => row.status === 'captured'
    ? {...row, outputAtMs: -1} : row)
  const report = summarizeFaultRecoveryCohort([record], 1)
  expect(report.counts.recovered_within_ten_seconds).toBe(0)
  expect(report.counts.not_recovered).toBe(1)
  expect(report.evaluation.passed).toBe(false)
})
