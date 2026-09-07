import {PostGainOutputAudit, type GainAuditMessage, type GainAuditMissingReason, type GainAuditSnapshot} from './post-gain-audit'

export type OutputArchiveEntry = Readonly<{atMs: number} & (
  {kind: 'message'; message: GainAuditMessage} |
  {kind: 'clock'; timestamp: AudioTimestamp; sampleRate: number})>
export type OutputArchiveSnapshot = Readonly<{
  method: 'post_gain_observation_replay_v1'; windowMs: number; retainedAfterMs: number
  lockedAtMs: number | null; closedAtMs: number | null; overflow: boolean; clockInvalid: boolean
  sourceMissing: GainAuditMissingReason | null; entries: readonly OutputArchiveEntry[]
}>
const validTime = (value: number) => Number.isFinite(value) && value >= 0
const copyEntry = (entry: OutputArchiveEntry): OutputArchiveEntry => entry.kind === 'clock'
  ? {...entry, timestamp: {contextTime: entry.timestamp.contextTime, performanceTime: entry.timestamp.performanceTime}}
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

export function replayPostGainOutput(archive: OutputArchiveSnapshot, bounds: {lowerMs: number; upperMs: number}): {
  complete: boolean; missingReason: string | null; audit: GainAuditSnapshot | null
} {
  const missing = (reason: string) => ({complete: false, missingReason: reason, audit: null})
  if (archive.method !== 'post_gain_observation_replay_v1' || archive.overflow || archive.clockInvalid
    || archive.sourceMissing !== null) return missing('output_archive_invalid')
  if (archive.closedAtMs === null) return missing('output_archive_not_closed')
  if (!validTime(bounds.lowerMs) || !validTime(bounds.upperMs) || bounds.lowerMs > bounds.upperMs
    || bounds.lowerMs < archive.retainedAfterMs || bounds.upperMs > archive.closedAtMs) return missing('output_archive_window_unobserved')
  const audit = new PostGainOutputAudit()
  let marked = false, previousAtMs = -1
  for (const entry of archive.entries) {
    if (!validTime(entry.atMs) || entry.atMs < previousAtMs || entry.atMs > archive.closedAtMs) return missing('output_archive_order_invalid')
    previousAtMs = entry.atMs
    // 当時の順序を再現してからcancelを挿入する。既存のtrackerへ過去の境界を後付けしない。
    if (!marked && entry.atMs > bounds.lowerMs) {audit.markCancelled(bounds, bounds.upperMs); marked = true}
    if (entry.kind === 'message') audit.record(entry.message)
    else audit.poll(entry.timestamp, entry.sampleRate, entry.atMs)
  }
  if (!marked) return missing('output_archive_window_unobserved')
  audit.close()
  const result = audit.snapshot()
  return {complete: result.complete, missingReason: result.missingReason, audit: result}
}
