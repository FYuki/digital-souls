import type {PacketOutputEvidence} from '../src/livekit/packet-output-diagnostic'

type Captured = Extract<PacketOutputEvidence, {status: 'captured'}>
type GapEvidence = {
  gap_samples: number; packet_index: number; packet_sample_offset: number; previous_packet_index: number
  receive_interval_upper_ms: number; decode_latency_upper_ms: number
  main_delivery_upper_ms: number; render_after_main_ms: number
}
export type PlaybackSupplySummary = {
  method: 'packet_receive_decode_main_render_bounds_v1'
  packet_count: number; rendered_samples: number; gap_count: number; gap_samples: number
  maximum_gap_samples: number; missing_observations: number; gap_evidence_overflow: boolean
  maximum_receive_interval_upper_ms: number; maximum_decode_latency_upper_ms: number
  maximum_main_delivery_upper_ms: number; maximum_render_after_main_ms: number
  gaps: GapEvidence[]
}
type DiagnosticWindow = typeof window & {
  __digitalSoulsVoiceSessionTestPort?: {bindRoom?: (room: {
    setPacketOutputObserver: (observe: (row: PacketOutputEvidence) => void) => void
  }) => void}
  __voicePlaybackSupply?: {summaries: Record<string, PlaybackSupplySummary>; overflow: boolean}
}

// page.evaluateでも実行するため、型以外の外側の変数を参照しない。
// 直前packetだけを保持し、gapの前後は16件まで。PCM・本文は受け取らない。
export function installPlaybackSupplyDiagnostic(): void {
  const target = window as DiagnosticWindow
  const port = target.__digitalSoulsVoiceSessionTestPort
  if (!port) throw new Error('voice diagnostic port unavailable')
  const diagnostic = {summaries: {} as Record<string, PlaybackSupplySummary>, overflow: false}
  target.__voicePlaybackSupply = diagnostic
  const previous = new Map<string, Captured>()
  const bind = port.bindRoom
  port.bindRoom = room => {
    bind?.(room)
    room.setPacketOutputObserver(row => {
      let summary = diagnostic.summaries[row.responseId]
      if (!summary) {
        if (Object.keys(diagnostic.summaries).length >= 4) {diagnostic.overflow = true; return}
        summary = diagnostic.summaries[row.responseId] = {
          method: 'packet_receive_decode_main_render_bounds_v1', packet_count: 0, rendered_samples: 0,
          gap_count: 0, gap_samples: 0, maximum_gap_samples: 0, missing_observations: 0,
          gap_evidence_overflow: false, maximum_receive_interval_upper_ms: 0,
          maximum_decode_latency_upper_ms: 0, maximum_main_delivery_upper_ms: 0,
          maximum_render_after_main_ms: 0, gaps: [],
        }
      }
      if (row.status !== 'captured') {summary.missing_observations++; return}
      const {packet, interval} = row
      const mainAt = packet.mainReceivedAtMs
      if (mainAt === undefined || ![mainAt, packet.receivedAtBoundsMs.lowerMs, packet.receivedAtBoundsMs.upperMs,
        packet.decodedAtBoundsMs.lowerMs, packet.decodedAtBoundsMs.upperMs, row.outputAtMs,
        interval.startFrame, interval.endFrame].every(value => Number.isFinite(value) && value >= 0)
        || mainAt < packet.decodedAtBoundsMs.lowerMs || row.outputAtMs < mainAt
        || interval.endFrame <= interval.startFrame) {summary.missing_observations++; return}
      const prior = previous.get(row.responseId)
      if (prior && interval.startFrame < prior.interval.endFrame) {summary.missing_observations++; return}
      const receiveInterval = prior && packet.packetIndex !== prior.packet.packetIndex
        ? Math.max(0, packet.receivedAtBoundsMs.upperMs - prior.packet.receivedAtBoundsMs.lowerMs) : 0
      const decodeLatency = packet.decodedAtBoundsMs.upperMs - packet.receivedAtBoundsMs.lowerMs
      const mainDelivery = mainAt - packet.decodedAtBoundsMs.lowerMs
      const renderAfterMain = row.outputAtMs - mainAt
      if (interval.packetSampleOffset === 0) {
        summary.packet_count++
        summary.maximum_receive_interval_upper_ms = Math.max(summary.maximum_receive_interval_upper_ms, receiveInterval)
        summary.maximum_decode_latency_upper_ms = Math.max(summary.maximum_decode_latency_upper_ms, decodeLatency)
        summary.maximum_main_delivery_upper_ms = Math.max(summary.maximum_main_delivery_upper_ms, mainDelivery)
        summary.maximum_render_after_main_ms = Math.max(summary.maximum_render_after_main_ms, renderAfterMain)
      }
      summary.rendered_samples += interval.endFrame - interval.startFrame
      const gap = prior ? interval.startFrame - prior.interval.endFrame : 0
      if (gap > 0 && prior) {
        summary.gap_count++; summary.gap_samples += gap
        summary.maximum_gap_samples = Math.max(summary.maximum_gap_samples, gap)
        if (summary.gaps.length < 16) summary.gaps.push({gap_samples: gap, packet_index: packet.packetIndex,
          packet_sample_offset: interval.packetSampleOffset, previous_packet_index: prior.packet.packetIndex,
          receive_interval_upper_ms: receiveInterval, decode_latency_upper_ms: decodeLatency,
          main_delivery_upper_ms: mainDelivery, render_after_main_ms: renderAfterMain})
        else summary.gap_evidence_overflow = true
      }
      previous.set(row.responseId, row)
    })
  }
}

export function readPlaybackSupplyDiagnostic(responseId: string): {summary: PlaybackSupplySummary | null; overflow: boolean} {
  const diagnostic = (window as DiagnosticWindow).__voicePlaybackSupply
  return {summary: diagnostic?.summaries[responseId] ?? null, overflow: diagnostic?.overflow ?? false}
}
