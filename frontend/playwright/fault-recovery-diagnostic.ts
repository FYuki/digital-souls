import type {AudioProbeObservation} from '../src/livekit/audio-probe'
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
      probeAudio: () => Promise<AudioProbeObservation>;
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

// 新規診断は復旧後のnonce・世代・全出力を検証する。保存済みstatusだけでは成功にしない。
export function analyzeProbeFaultRecovery(restored: TimeBounds, probes: readonly TimedProbe[],
  packets: readonly PacketOutputEvidence[], overflow: boolean, outputPathFailures: number, value: unknown) {
  const base = analyzeFaultRecovery(restored, probes, packets, overflow, outputPathFailures)
  const failed = () => ({...base, audio_recovery_upper_ms: null, recovery_upper_ms: null,
    output_evidence_complete: false, packet_evidence_missing: base.packet_evidence_missing + 1,
    audio_missing_reason: 'fresh_audio_probe_incomplete'})
  if (!value || typeof value !== 'object') return failed()
  const probe = value as AudioProbeObservation, completion = probe.completion
  if (probe.scope !== 'rtc_audio_probe' || probe.status !== 'captured' || probe.reason !== undefined
    || probe.cleanupCompleted !== true || !completion || !Array.isArray(probe.packetOutputs)
    || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(probe.probeId)
    || !Number.isFinite(probe.requestedAtMs) || probe.requestedAtMs <= restored.upperMs
    || !Number.isFinite(probe.completedAtMs) || probe.completedAtMs < probe.requestedAtMs
    || !probes.some(p => p.status === 'received' && p.generation === probe.generation
      && p.sentAtMs !== null && p.receivedAtMs !== null && p.sentAtMs > restored.upperMs
      && p.receivedAtMs >= p.sentAtMs && p.receivedAtMs <= probe.completedAtMs)
    || completion.expectedSamples !== 10560 || completion.inputSamples !== 9600 || completion.paddingSamples !== 960
    || completion.renderedSamples !== 10560 || completion.packetCount !== 11 || completion.gapSamples !== 0
    || completion.maximumGapSamples !== 0 || completion.gapCount !== 0 || completion.sampleRate !== 48000) return failed()
  let samples = 0, endFrame: number | undefined, source: number | undefined, firstRtp: number | undefined
  for (const row of probe.packetOutputs) {
    if (row.status !== 'captured' || row.responseId !== probe.probeId || row.trackSid !== probe.trackSid
      || row.generation !== probe.generation || row.packet.packetIndex !== Math.floor(samples / 960)
      || row.interval.packetIndex !== row.packet.packetIndex || row.interval.packetSampleOffset !== samples % 960
      || row.packet.receivedAtBoundsMs.lowerMs <= probe.requestedAtMs || row.confirmedAtMs > probe.completedAtMs
      || (endFrame !== undefined && row.interval.startFrame !== endFrame)
      || (source !== undefined && row.packet.source !== source)) return failed()
    if (!Number.isInteger(row.packet.rtpTimestamp) || row.packet.rtpTimestamp < 0 || row.packet.rtpTimestamp > 0xffffffff
      || !Number.isInteger(row.packet.source) || row.packet.source < 0 || row.packet.source > 0xffffffff) return failed()
    firstRtp ??= Number(row.packet.rtpTimestamp)
    if (((row.packet.rtpTimestamp - firstRtp) >>> 0) !== Math.floor(samples / 960) * 960) return failed()
    if (samples === 0 && row.interval.startFrame !== completion.firstOutputFrame) return failed()
    samples += row.interval.endFrame - row.interval.startFrame
    endFrame = row.interval.endFrame; source = row.packet.source
  }
  if (samples !== 10560 || endFrame !== completion.lastOutputEndFrame
    || completion.firstRtpTimestamp !== firstRtp || completion.lastRtpTimestamp !== ((firstRtp! + 9600) >>> 0)) return failed()
  // 状態同期で世代が進んだ場合、音声と同世代の制御往復を採る。
  // controlとaudioの実証順は問わず、両方の成立時刻の遅い側を復旧時間とする。
  const matchingControls = probes.filter(p => p.generation === probe.generation
    && p.receivedAtMs !== null && p.receivedAtMs <= probe.completedAtMs)
  const audio = analyzeFaultRecovery(restored, matchingControls, probe.packetOutputs, false, 0)
  const combined = analyzeFaultRecovery(restored, probes, [...packets, ...probe.packetOutputs], overflow, outputPathFailures)
  return {...combined, control_recovery_upper_ms: audio.control_recovery_upper_ms,
    audio_recovery_upper_ms: audio.audio_recovery_upper_ms,
    recovery_upper_ms: audio.control_recovery_upper_ms === null || audio.audio_recovery_upper_ms === null
      ? null : Math.max(audio.control_recovery_upper_ms, audio.audio_recovery_upper_ms),
    audio_missing_reason: audio.audio_missing_reason}
}

export async function measureFaultRecovery(page: Page, runner: FaultClockRunner,
  before: FaultClockCalibration, record: Record<string, unknown>): Promise<boolean> {
  const events: FaultEvent[] = [], probes: TimedProbe[] = []
  record.fault_events = events; record.fault_probes = probes
  record.fault_duration_ms = 2000
  record.audio_availability_method = 'fresh_rtc_probe_and_followup'
  let audioProbe: Promise<void> | undefined
  let endedAt: number | undefined, pulseFailed = false
  // 例外時にも復旧finallyが終了するまで待つ。pageが閉じてもpulseを中断しない。
  const pulse = runner.pulse(event => events.push(event)).then(() => {endedAt = performance.now()},
    () => {pulseFailed = true})
  try {
    while (!pulseFailed && (endedAt === undefined || performance.now() - endedAt < 10000)) {
      const sentAfterPulse = endedAt !== undefined
      const control = await page.evaluate(async () => ({
        ...await window.__voiceControlProbeRoom!.probeControl(), observedAtMs: performance.now(),
      }))
      probes.push(control)
      if (sentAfterPulse && control.status === 'received' && audioProbe === undefined) {
        audioProbe = page.evaluate(() => window.__voiceControlProbeRoom!.probeAudio())
          .then(observation => {record.audio_probe = observation}, () => {record.audio_probe = null})
      }
      await page.waitForTimeout(100)
    }
  } finally {await pulse; await audioProbe}
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
  const recovery = analyzeProbeFaultRecovery(restored, probes, evidence.packets, evidence.overflow, evidence.outputPathFailures, record.audio_probe)
  record.recovery = recovery
  const passed = affected && recovery.recovery_upper_ms !== null && !recovery.packet_evidence_missing
    && recovery.output_evidence_complete && !recovery.duplicate_packet_output_intervals
    && recovery.recovery_upper_ms <= 3000
  record.fault_recovery_passed = passed
  return passed
}
