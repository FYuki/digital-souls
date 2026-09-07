import type {Page} from '@playwright/test'
import type {} from './voice-chat-suite'
import type {ControlProbeObservation} from '../src/livekit/control-probe'
import type {PacketOutputEvidence} from '../src/livekit/packet-output-diagnostic'
import {faultTimeInBrowser, faultToBrowserOffset, recoveryLatencyUpperMs,
  type TimeBounds, type FaultClockCalibration} from './fault-clock'
import {calibrateFaultClock, type FaultClockRunner, type FaultEvent} from './fault-clock-runner'

declare global {
  interface Window {
    __voiceControlProbeRoom?: {probeControl: () => Promise<ControlProbeObservation>;
      setPacketOutputObserver: (observer: (row: PacketOutputEvidence) => void) => void}
    __voicePacketOutputs?: PacketOutputEvidence[]
    __voicePacketOutputOverflow?: boolean
  }
}

export type TimedProbe = ControlProbeObservation & {observedAtMs: number}

export function analyzeFaultRecovery(restored: TimeBounds, probes: readonly TimedProbe[],
  packets: readonly PacketOutputEvidence[], overflow: boolean, outputPathFailures: number) {
  const validTime = (value: number) => Number.isFinite(value) && value >= 0
  if (![restored.lowerMs, restored.upperMs].every(validTime) || restored.lowerMs > restored.upperMs) {
    throw new Error('invalid restoration interval')
  }
  if (!Number.isInteger(outputPathFailures) || outputPathFailures < 0) throw new Error('invalid output path coverage')
  const control = probes.filter(p => p.status === 'received' && p.probeId !== null
    && p.sentAtMs !== null && p.receivedAtMs !== null && validTime(p.sentAtMs) && validTime(p.receivedAtMs)
    && p.sentAtMs > restored.upperMs && p.receivedAtMs >= p.sentAtMs)
    .map(p => recoveryLatencyUpperMs(restored, p.sentAtMs!, p.receivedAtMs!))
    .filter(ms => ms <= 10000)
  const audio: number[] = []
  let duplicates = 0, missing = overflow ? 1 : 0
  const ranges = new Map<string, Array<[number, number]>>()
  for (const row of packets) {
    if (row.status !== 'captured') {missing++; continue}
    const {packet, interval} = row
    const start = interval.packetSampleOffset, end = start + interval.endFrame - interval.startFrame
    const received = packet.receivedAtBoundsMs, decoded = packet.decodedAtBoundsMs
    if (![row.outputAtMs, row.confirmedAtMs, received.lowerMs, received.upperMs,
      decoded.lowerMs, decoded.upperMs].every(validTime)
      || received.lowerMs > received.upperMs || decoded.lowerMs > decoded.upperMs
      || received.upperMs - received.lowerMs > 1 || decoded.upperMs - decoded.lowerMs > 1
      || received.lowerMs > decoded.lowerMs || decoded.lowerMs > row.outputAtMs
      || row.outputAtMs + (end - start) / 48 > row.confirmedAtMs || !Number.isInteger(start) || !Number.isInteger(end)
      || start < 0 || end > 960 || start >= end || packet.rtpTimestamp !== interval.rtpTimestamp) {
      missing++; continue
    }
    const key = `${row.responseId}:${packet.source}:${packet.rtpTimestamp}`
    const prior = ranges.get(key) ?? []
    if (prior.some(([a, b]) => start < b && a < end)) duplicates++
    prior.push([start, end]); ranges.set(key, prior)
    // 受信済みbufferや無音だけをaudio回復に数えない。
    const audible = row.firstAudibleAtMs
    if (audible !== undefined && validTime(audible) && audible >= row.outputAtMs && audible <= row.confirmedAtMs
      && interval.energy > 0 && received.lowerMs > restored.upperMs
      && audible - restored.lowerMs <= 10000) audio.push(audible - restored.lowerMs)
  }
  const controlMs = control.length ? Math.min(...control) : null
  const audioMs = audio.length ? Math.min(...audio) : null
  return {control_recovery_upper_ms: controlMs, audio_recovery_upper_ms: audioMs,
    recovery_upper_ms: controlMs === null || audioMs === null ? null : Math.max(controlMs, audioMs),
    duplicate_packet_output_intervals: duplicates, packet_evidence_missing: missing,
    output_path_failures: outputPathFailures,
    output_evidence_complete: packets.length > 0 && !missing && outputPathFailures === 0,
    duplicate_measurement_scope: 'overlapping_response_ssrc_rtp_sample_intervals',
    audio_missing_reason: audioMs === null ? 'no_post_restore_received_audible_output' : null}
}

export async function measureFaultRecovery(page: Page, runner: FaultClockRunner,
  before: FaultClockCalibration, record: Record<string, unknown>): Promise<boolean> {
  const events: FaultEvent[] = [], probes: TimedProbe[] = []
  record.fault_events = events; record.fault_probes = probes
  record.fault_duration_ms = 2000
  let endedAt: number | undefined, pulseFailed = false
  // 例外時にも復旧finallyが終了するまで待つ。pageが閉じてもpulseを中断しない。
  const pulse = runner.pulse(event => events.push(event)).then(() => {endedAt = performance.now()},
    () => {pulseFailed = true})
  try {
    while (!pulseFailed && (endedAt === undefined || performance.now() - endedAt < 10000)) {
      probes.push(await page.evaluate(async () => ({
        ...await window.__voiceControlProbeRoom!.probeControl(), observedAtMs: performance.now(),
      })))
      await page.waitForTimeout(100)
    }
  } finally {await pulse}
  record.fault_operation_succeeded = !pulseFailed
  const after = await calibrateFaultClock(page, runner)
  record.fault_clock_after = after
  const offset = faultToBrowserOffset(before, after)
  record.fault_to_browser_offset_ms = offset
  if (pulseFailed) throw new Error('dedicated fault operation failed')
  const times = Object.fromEntries(events.map(event => [event.event, faultTimeInBrowser(event.timestamp_ns, offset)]))
  record.fault_event_browser_bounds_ms = times
  // 復旧操作の開始～実TCP疎通成功を囲む。時計誤差とは別に操作中の不確実区間を残す。
  const restored = {lowerMs: times.network_link_restore_started.lowerMs,
    upperMs: times.signaling_tcp_reachable.upperMs}
  record.network_restored_bounds_ms = restored
  const affected = probes.some(p => (p.status === 'timeout' || p.status === 'send_failed')
    && p.sentAtMs !== null && p.sentAtMs > times.network_link_disconnected.upperMs
    && p.observedAtMs < times.network_link_restore_started.lowerMs)
  record.network_fault_affected_control = affected
  const evidence = await page.evaluate(() => ({packets: window.__voicePacketOutputs ?? [],
    overflow: window.__voicePacketOutputOverflow ?? false,
    outputPathFailures: (window.__voiceChatE2E.transportFailures ?? []).filter(failure =>
      ['rtp_timeline', 'renderer', 'output_clock', 'media_decoder', 'audio_graph'].includes(failure.stage)).length}))
  const recovery = analyzeFaultRecovery(restored, probes, evidence.packets, evidence.overflow, evidence.outputPathFailures)
  record.recovery = recovery
  const passed = affected && recovery.recovery_upper_ms !== null && !recovery.packet_evidence_missing
    && recovery.output_evidence_complete && !recovery.duplicate_packet_output_intervals
    && recovery.recovery_upper_ms <= 3000
  record.fault_recovery_passed = passed
  return passed
}
