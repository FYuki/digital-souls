import {analyzeFaultRecovery, analyzeProbeFaultRecovery, type TimedProbe} from './fault-recovery-diagnostic'
import {faultToBrowserOffset, faultTimeInBrowser, type FaultClockCalibration} from './fault-clock'
import type {PacketOutputEvidence} from '../src/livekit/packet-output-diagnostic'

const eventsInOrder = ['network_link_disconnected', 'network_link_restore_started',
  'network_link_restored', 'signaling_tcp_reachable'] as const
const uuid = (value: unknown): value is string => typeof value === 'string'
  && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value)
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('record unavailable')
  return value as Record<string, unknown>
}
const quantile = (input: readonly number[], probability: number): number | null => {
  if (!input.length) return null
  const values = [...input].sort((a, b) => a - b), index = (values.length - 1) * probability
  const lower = Math.floor(index)
  return values[lower] + (values[Math.ceil(index)] - values[lower]) * (index - lower)
}

// 保存済みの合否・recovery値は信用せず、因果時計とprobe・実出力から全試行を再計算する。
export function summarizeFaultRecoveryCohort(records: readonly Record<string, unknown>[], expected: number) {
  if (!Number.isInteger(expected) || expected < 1 || expected > 100 || records.length !== expected) {
    throw new Error('all expected reconnect trials must be recorded')
  }
  const sessions = new Set<string>(), conversations = new Set<string>(), fixtures = new Set<string>(), revisions = new Set<string>()
  const audioMethods = new Set<string>()
  const control: number[] = [], audio: number[] = [], recovered: number[] = []
  let verifiedFaults = 0, completeOutput = 0, duplicates = 0, ended = 0, childrenClosed = 0, followups = 0
  const missing: Record<string, number> = {}
  const omit = (reason: string) => {missing[reason] = (missing[reason] ?? 0) + 1}
  for (const record of records) {
    if (record.measurement_scope !== 'livekit_fault_recovery_session_diagnostic') {
      throw new Error('reconnect cohort requires actual fault trials')
    }
    if (typeof record.fixture_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(record.fixture_sha256)) {
      throw new Error('reconnect fixture identity unavailable')
    }
    fixtures.add(record.fixture_sha256)
    // 障害注入前の失敗には方式がまだない。旧packet方式と推定して混在扱いにせず、失敗分母には残す。
    const audioMethod = record.audio_availability_method
      ?? (record.fault_operation_succeeded === true ? 'response_packets' : null)
    if (audioMethod !== null) {
      if (!['response_packets', 'fresh_rtc_probe_and_followup'].includes(String(audioMethod))) throw new Error('unknown audio availability method')
      audioMethods.add(String(audioMethod))
    }
    if (typeof record.measurement_revision === 'string' && /^[0-9a-f]{40}$/.test(record.measurement_revision)) revisions.add(record.measurement_revision)
    else omit('measurement_revision_unavailable')
    if (uuid(record.session_id)) {
      if (sessions.has(record.session_id)) throw new Error('reconnect sessions must be independent')
      sessions.add(record.session_id)
    }
    const initial = record.initial_cycle ? object(record.initial_cycle) : null
    if (initial && initial.sessionId !== record.session_id) throw new Error('initial session correlation mismatch')
    if (initial && uuid(initial.conversationId)) {
      if (conversations.has(initial.conversationId)) throw new Error('reconnect conversations must be independent')
      conversations.add(initial.conversationId)
    }
    if (record.session_end_confirmed === true) ended++
    if (record.fault_clock_process_closed === true) childrenClosed++
    if (record.post_fault_followup) {
      const followup = object(record.post_fault_followup)
      if (followup.outcome === 'success' && initial) {
        const cycle = object(followup.cycle), completion = object(followup.playback_completion)
        if (cycle.sessionId === record.session_id && cycle.conversationId === initial.conversationId
          && uuid(cycle.responseId) && cycle.responseId !== initial.responseId
          && followup.track_response_matches === true
          && Number.isSafeInteger(completion.expectedSamples) && Number(completion.expectedSamples) > 0
          && completion.expectedSamples === completion.renderedSamples
          && completion.gapSamples === 0 && completion.maximumGapSamples === 0) followups++
      }
    }
    if (record.fault_operation_succeeded !== true) {omit('fault_operation_unverified'); continue}
    try {
      const offset = faultToBrowserOffset(record.fault_clock_before as FaultClockCalibration,
        record.fault_clock_after as FaultClockCalibration)
      if (!Array.isArray(record.fault_events) || record.fault_events.length !== eventsInOrder.length) {
        throw new Error('fault events unavailable')
      }
      const timestamps = record.fault_events.map((value, index) => {
        const row = object(value)
        if (row.event !== eventsInOrder[index] || typeof row.timestamp_ns !== 'string') throw new Error('fault event order invalid')
        return {ns: BigInt(row.timestamp_ns), bounds: faultTimeInBrowser(row.timestamp_ns, offset)}
      })
      if (timestamps.some((row, i) => i > 0 && row.ns < timestamps[i - 1].ns)
        || timestamps[1].ns - timestamps[0].ns < 2000000000n || record.fault_duration_ms !== 2000) {
        throw new Error('fault duration invalid')
      }
      const restored = {lowerMs: timestamps[1].bounds.lowerMs, upperMs: timestamps[3].bounds.upperMs}
      if (!Array.isArray(record.fault_probes)) throw new Error('control observations unavailable')
      const probes = record.fault_probes as TimedProbe[]
      const affected = probes.some(p => (p.status === 'timeout' || p.status === 'send_failed')
        && typeof p.sentAtMs === 'number' && Number.isFinite(p.sentAtMs)
        && typeof p.observedAtMs === 'number' && Number.isFinite(p.observedAtMs)
        && p.sentAtMs > timestamps[0].bounds.upperMs && p.observedAtMs < timestamps[1].bounds.lowerMs)
      if (!affected) {omit('network_effect_unverified'); continue}
      verifiedFaults++
      const evidence = object(record.evidence)
      if (!Array.isArray(evidence.packet_outputs) || typeof evidence.packet_output_overflow !== 'boolean'
        || !Array.isArray(evidence.transport_failures)) throw new Error('output observations unavailable')
      const outputFailures = evidence.transport_failures.filter(value =>
        ['rtp_timeline', 'renderer', 'output_clock', 'media_decoder', 'audio_graph'].includes(String(object(value).stage))).length
      const result = audioMethod === 'fresh_rtc_probe_and_followup'
        ? analyzeProbeFaultRecovery(restored, probes, evidence.packet_outputs as PacketOutputEvidence[],
          evidence.packet_output_overflow, outputFailures, record.audio_probe)
        : analyzeFaultRecovery(restored, probes, evidence.packet_outputs as PacketOutputEvidence[],
          evidence.packet_output_overflow, outputFailures)
      duplicates += result.duplicate_packet_output_intervals
      if (result.control_recovery_upper_ms !== null) control.push(result.control_recovery_upper_ms)
      if (result.audio_recovery_upper_ms !== null) audio.push(result.audio_recovery_upper_ms)
      if (result.output_evidence_complete) {
        completeOutput++
        if (result.recovery_upper_ms !== null) recovered.push(result.recovery_upper_ms)
      } else omit('output_evidence_incomplete')
    } catch {omit('recovery_evidence_invalid')}
  }
  if (audioMethods.size > 1) throw new Error('reconnect cohort mixes audio availability methods')
  if (revisions.size > 1) throw new Error('reconnect cohort mixes revisions')
  if (fixtures.size !== 1) throw new Error('reconnect cohort mixes fixtures')
  const coverage = sessions.size === expected && conversations.size === expected && verifiedFaults === expected
    && completeOutput === expected && ended === expected && childrenClosed === expected && !Object.keys(missing).length
  const ratePassed = recovered.length * 100 >= expected * 99
  const p95 = quantile(recovered, .95)
  return {schema_version: '1.0', measurement_scope: 'livekit_fault_recovery_cohort_report',
    audio_availability_method: [...audioMethods][0] ?? null,
    fixture_sha256: [...fixtures][0], measurement_revision: [...revisions][0] ?? null,
    counts: {expected, recorded: records.length, independent_sessions: sessions.size, independent_conversations: conversations.size,
      verified_faults: verifiedFaults, complete_output_trials: completeOutput, recovered_within_ten_seconds: recovered.length,
      not_recovered: expected - recovered.length, session_end_confirmed: ended, fault_children_closed: childrenClosed,
      successful_followup_conversations: followups},
    missing_reasons: missing, duplicate_packet_output_intervals: duplicates,
    duplicate_measurement_scope: 'overlapping_response_ssrc_rtp_sample_intervals',
    latency_ms: {control: {count: control.length, p50: quantile(control, .5), p95: quantile(control, .95)},
      audio: {count: audio.length, p50: quantile(audio, .5), p95: quantile(audio, .95)},
      both: {count: recovered.length, p50: quantile(recovered, .5), p95}},
    evaluation: {coverage_complete: coverage, rate_passed: ratePassed, latency_passed: p95 !== null && p95 <= 3000,
      passed: expected === 100 && coverage && ratePassed && p95 !== null && p95 <= 3000
        && duplicates === 0 && followups === expected}}
}
