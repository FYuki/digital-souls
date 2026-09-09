import {PostGainOutputAudit, type GainAuditMessage, type GainAuditMissingReason, type GainAuditSnapshot} from './post-gain-audit'

export type OutputArchiveEntry = Readonly<{atMs: number} & (
  {kind: 'message'; message: GainAuditMessage} |
  {kind: 'clock'; timestamp: AudioTimestamp; sampleRate: number} |
  {kind: 'stop_requested'; requestId: string; sessionId: string; responseId: string; generation: number; graphId: string} |
  {kind: 'stop_marker'; endFrame: number} |
  {kind: 'stop_confirmed'; requestId: string; endFrame: number; outputClockPassedFrame: number})>
export type OutputArchiveSnapshot = Readonly<{
  method: 'post_gain_observation_replay_v1'; windowMs: number; retainedAfterMs: number
  lockedAtMs: number | null; closedAtMs: number | null; overflow: boolean; clockInvalid: boolean
  sourceMissing: GainAuditMissingReason | null; entries: readonly OutputArchiveEntry[]
}>
const validTime = (value: number) => Number.isFinite(value) && value >= 0
const copyEntry = (entry: OutputArchiveEntry): OutputArchiveEntry => entry.kind === 'clock'
  ? {kind: 'clock', atMs: entry.atMs, sampleRate: entry.sampleRate,
    timestamp: {contextTime: entry.timestamp.contextTime, performanceTime: entry.timestamp.performanceTime}}
  : entry.kind === 'stop_requested' ? {kind: entry.kind, atMs: entry.atMs, requestId: entry.requestId,
    sessionId: entry.sessionId, responseId: entry.responseId, generation: entry.generation, graphId: entry.graphId}
  : entry.kind === 'stop_marker' ? {kind: entry.kind, atMs: entry.atMs, endFrame: entry.endFrame}
  : entry.kind === 'stop_confirmed' ? {kind: entry.kind, atMs: entry.atMs, requestId: entry.requestId,
    endFrame: entry.endFrame, outputClockPassedFrame: entry.outputClockPassedFrame}
  : {atMs: entry.atMs, kind: 'message', message: entry.message.kind === 'output'
    ? {kind: 'output', confirmedFrame: entry.message.confirmedFrame, intervals: entry.message.intervals.map(row => ({
      startFrame: row.startFrame, endFrame: row.endFrame, nonzeroSamples: row.nonzeroSamples,
      firstNonzeroFrame: row.firstNonzeroFrame, lastNonzeroFrame: row.lastNonzeroFrame}))}
    : entry.message.kind === 'finished' ? {kind: 'finished', endFrame: entry.message.endFrame}
      : {kind: 'missing', reason: entry.message.reason}}

// 本文/PCMを保持せず、サーバーcancel時刻を後から照合するための直近4秒だけを残す。
export class PostGainOutputArchive {
  private entries: OutputArchiveEntry[] = []
  private readonly windowMs = 4000
  private retainedAfterMs = 0
  private lockedAtMs: number | null = null
  private closedAtMs: number | null = null
  private lastAtMs: number | null = null
  private overflow = false
  private clockInvalid = false
  private sourceMissing: GainAuditMissingReason | null = null

  record(entry: OutputArchiveEntry): void {
    if (this.closedAtMs !== null) return
    if (!this.observeTime(entry.atMs)) return
    if (entry.kind === 'message' && entry.message.kind === 'missing') this.sourceMissing ??= entry.message.reason
    if (this.lockedAtMs === null) {
      this.retainedAfterMs = Math.max(0, entry.atMs - this.windowMs)
      while (this.entries.length && this.entries[0].atMs < this.retainedAfterMs) this.entries.shift()
    }
    if (this.entries.length >= 4096) {this.overflow = true; return}
    this.entries.push(copyEntry(entry))
  }
  lock(atMs: number): void {
    if (!this.observeTime(atMs) || this.closedAtMs !== null) return
    this.lockedAtMs ??= atMs
  }
  close(atMs: number): void {
    if (this.closedAtMs !== null) return
    this.observeTime(atMs); this.closedAtMs = atMs
  }
  fail(reason: GainAuditMissingReason): void {this.sourceMissing ??= reason}
  snapshot(): OutputArchiveSnapshot {
    return {method: 'post_gain_observation_replay_v1', windowMs: this.windowMs, retainedAfterMs: this.retainedAfterMs,
      lockedAtMs: this.lockedAtMs, closedAtMs: this.closedAtMs, overflow: this.overflow, clockInvalid: this.clockInvalid,
      sourceMissing: this.sourceMissing, entries: this.entries.map(copyEntry)}
  }
  private observeTime(atMs: number): boolean {
    if (!validTime(atMs) || (this.lastAtMs !== null && atMs < this.lastAtMs)) {this.clockInvalid = true; return false}
    this.lastAtMs = atMs; return true
  }
}

export function replayPostGainOutput(archive: OutputArchiveSnapshot, bounds: {lowerMs: number; upperMs: number},
  confirmedOutputFrameFloor?: number): {
  complete: boolean; missingReason: string | null; audit: GainAuditSnapshot | null
} {
  const missing = (reason: string) => ({complete: false, missingReason: reason, audit: null})
  if (archive.method !== 'post_gain_observation_replay_v1' || archive.overflow || archive.clockInvalid
    || archive.sourceMissing !== null) return missing('output_archive_invalid')
  if (archive.closedAtMs === null) return missing('output_archive_not_closed')
  if (!validTime(archive.closedAtMs) || !validTime(archive.retainedAfterMs) || archive.windowMs !== 4000
    || (archive.lockedAtMs !== null && (!validTime(archive.lockedAtMs) || archive.lockedAtMs > archive.closedAtMs))
    || !Array.isArray(archive.entries) || archive.entries.length > 4096) return missing('output_archive_invalid')
  if (!validTime(bounds.lowerMs) || !validTime(bounds.upperMs) || bounds.lowerMs > bounds.upperMs
    || bounds.lowerMs < archive.retainedAfterMs || bounds.upperMs > archive.closedAtMs) return missing('output_archive_window_unobserved')
  const audit = new PostGainOutputAudit()
  let marked = false, previousAtMs = -1
  for (const entry of archive.entries) {
    if (!validTime(entry.atMs) || entry.atMs < previousAtMs || entry.atMs > archive.closedAtMs) return missing('output_archive_order_invalid')
    previousAtMs = entry.atMs
    // 当時の順序を再現してからcancelを挿入する。既存のtrackerへ過去の境界を後付けしない。
    if (!marked && entry.atMs > bounds.lowerMs) {audit.markCancelled(bounds, bounds.upperMs, confirmedOutputFrameFloor); marked = true}
    if (entry.kind === 'message') audit.record(entry.message)
    else if (entry.kind === 'clock') audit.poll(entry.timestamp, entry.sampleRate, entry.atMs)
    else if (!['stop_requested', 'stop_marker', 'stop_confirmed'].includes(entry.kind)) return missing('output_archive_invalid')
  }
  if (!marked) return missing('output_archive_window_unobserved')
  audit.close()
  const result = audit.snapshot()
  return {complete: result.complete, missingReason: result.missingReason, audit: result}
}
