import {PostGainOutputAudit, type GainAuditInterval} from './post-gain-audit'
import {replayPostGainOutput} from './post-gain-archive'
import type {StaleAudioObservation} from './post-gain-monitor'

// 生IDとserver時計はprivate入力だけで扱い、公開集計へ出さない。
export type OutputStopProof = Readonly<{
  sessionId: string; responseId: string; generation: number; graphId: string; requestId: string
  // requested, source stopped, confirmed, Core cancel lower, upper。JS整数精度に依存しない。
  serverOrderNs: readonly string[]
}>
const identifier = (v: unknown): v is string => typeof v === 'string'
  && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v)
const frame = (v: number) => Number.isSafeInteger(v) && v >= 0

export function replayConfirmedOutputStop(output: StaleAudioObservation,
  bounds: {lowerMs: number; upperMs: number}, proof: OutputStopProof | null) {
  const invalid = () => ({complete: false, missingReason: 'output_stop_proof_unverified', audit: null,
    stopProofVerified: false as const})
  const base = replayPostGainOutput(output.outputArchive, bounds)
  if (!base.complete) return {...base, stopProofVerified: false as const}
  if (proof === null || !output.graphClosed || !identifier(proof.requestId) || !identifier(proof.graphId)
    || !identifier(proof.sessionId) || !identifier(proof.responseId)
    || proof.sessionId !== output.sessionId || proof.responseId !== output.responseId
    || proof.generation !== output.generation || !frame(proof.generation) || proof.graphId !== output.outputGraphId
    || !Array.isArray(proof.serverOrderNs) || proof.serverOrderNs.length !== 5
    || !proof.serverOrderNs.every(value => typeof value === 'string' && /^(0|[1-9][0-9]*)$/.test(value))) return invalid()
  const server = proof.serverOrderNs.map(value => BigInt(value))
  if (server.some((value, i) => i > 0 && value < server[i - 1])) return invalid()
  const archive = output.outputArchive, audit = new PostGainOutputAudit()
  let requestedAt: number | null = null, requestedAfterFrame: number | null = null
  let marker: number | null = null, confirmedAt: number | null = null
  let lastOutput: GainAuditInterval | undefined, lastClockAt: number | null = null
  for (const entry of archive.entries) {
    if (entry.kind === 'message') {
      audit.record(entry.message)
      if (entry.message.kind === 'output') {
        if (marker !== null && entry.message.intervals.some(row => row.endFrame > marker! && row.nonzeroSamples !== 0)) return invalid()
        lastOutput = entry.message.intervals.at(-1)
      }
    } else if (entry.kind === 'clock') {
      audit.poll(entry.timestamp, entry.sampleRate, entry.atMs)
      lastClockAt = entry.atMs
    } else if (entry.kind === 'stop_requested') {
      if (requestedAt !== null || entry.requestId !== proof.requestId || entry.graphId !== proof.graphId
        || entry.sessionId !== proof.sessionId || entry.responseId !== proof.responseId || entry.generation !== proof.generation) return invalid()
      requestedAt = entry.atMs
      requestedAfterFrame = lastOutput?.endFrame ?? null
    } else if (entry.kind === 'stop_marker') {
      if (requestedAt === null || marker !== null || !frame(entry.endFrame)
        || lastOutput?.endFrame !== entry.endFrame || lastOutput.nonzeroSamples !== 0
        || (requestedAfterFrame !== null && entry.endFrame <= requestedAfterFrame)) return invalid()
      marker = entry.endFrame
    } else if (entry.kind === 'stop_confirmed') {
      const snapshot = audit.snapshot(), claimed = output.outputStopConfirmation
      if (marker === null || confirmedAt !== null || entry.requestId !== proof.requestId
        || !frame(entry.outputClockPassedFrame) || entry.endFrame !== marker
        || snapshot.outputClockPassedFrame !== entry.outputClockPassedFrame || entry.outputClockPassedFrame < marker
        || entry.atMs !== lastClockAt || snapshot.missingReason !== null
        || claimed?.endFrame !== marker || claimed.outputClockPassedFrame !== entry.outputClockPassedFrame
        || claimed.observedAtMs !== entry.atMs) return invalid()
      confirmedAt = entry.atMs
    } else return invalid()
  }
  audit.close()
  if (audit.snapshot().missingReason !== null || !audit.snapshot().drained || marker === null || confirmedAt === null
    || requestedAt === null || requestedAt < archive.retainedAfterMs || archive.lockedAtMs === null
    || confirmedAt > archive.lockedAtMs || confirmedAt - 0.2 > bounds.upperMs) return invalid()
  // 取消時間のboundsは変えず、取消前に実出力を通過したframeだけを下限へ加える。
  const result = replayPostGainOutput(archive, bounds, marker)
  return {...result, stopProofVerified: result.complete && result.missingReason === null}
}
