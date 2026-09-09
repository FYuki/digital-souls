export type DecodedReceiptSnapshot = Readonly<{
  sessionId: string; responseId: string; generation: number; boundary: 'decoded_packet_callback'
  beganAtMs: number; retainedAfterMs: number; cancelledAtMs: number | null; closedAtMs: number | null
  totalPackets: number; totalSamples: number; discardedPackets: number; discardedSamples: number
  overflow: boolean; missingReason: 'receipt_clock_invalid' | 'receipt_sample_invalid' | 'receipt_after_close' | null
  entries: readonly Readonly<{atMs: number; samples: number}>[]
}>
// audio graph生成前・切断後も、復号済みpacket callbackの寿命で受信を数える。
export class DecodedReceiptAudit {
  private rows: {atMs: number; samples: number}[] = []
  private retainedAfterMs = 0
  private cancelledAtMs: number | null = null
  private closedAtMs: number | null = null
  private totalPackets = 0
  private totalSamples = 0
  private discardedPackets = 0
  private discardedSamples = 0
  private overflow = false
  private missingReason: DecodedReceiptSnapshot['missingReason'] = null
  private lastAtMs: number
  constructor(readonly responseId: string, private readonly sessionId: string, private readonly generation: number,
    private readonly beganAtMs: number, private readonly report: (row: DecodedReceiptSnapshot) => void) {
    this.lastAtMs = beganAtMs
    if (!Number.isFinite(beganAtMs) || beganAtMs < 0) this.missingReason = 'receipt_clock_invalid'
  }
  received(samples: number, atMs: number): void {
    if (this.closedAtMs !== null) {this.missingReason = 'receipt_after_close'; this.emit(); return}
    if (!this.time(atMs)) {this.emit(); return}
    if (!Number.isSafeInteger(samples) || samples < 0) {this.missingReason = 'receipt_sample_invalid'; this.emit(); return}
    this.totalPackets++; this.totalSamples += samples
    if (!Number.isSafeInteger(this.totalSamples)) {this.overflow = true; this.emit(); return}
    if (this.cancelledAtMs === null) {
      this.retainedAfterMs = Math.max(0, atMs - 4000)
      while (this.rows.length && this.rows[0].atMs < this.retainedAfterMs) {
        this.discardedSamples += this.rows.shift()!.samples; this.discardedPackets++
      }
    }
    if (this.rows.length >= 4096) {this.overflow = true; this.emit(); return}
    this.rows.push({atMs, samples})
    if (this.cancelledAtMs !== null) this.emit()
  }
  cancel(atMs: number): void {
    if (!this.time(atMs)) {this.emit(); return}
    this.cancelledAtMs ??= atMs; this.emit()
  }
  close(atMs: number): void {
    if (this.closedAtMs !== null) return
    this.time(atMs); this.closedAtMs = atMs; this.emit()
  }
  snapshot(): DecodedReceiptSnapshot {
    return {sessionId: this.sessionId, responseId: this.responseId, generation: this.generation,
      boundary: 'decoded_packet_callback', beganAtMs: this.beganAtMs, retainedAfterMs: this.retainedAfterMs,
      cancelledAtMs: this.cancelledAtMs, closedAtMs: this.closedAtMs, overflow: this.overflow,
      totalPackets: this.totalPackets, totalSamples: this.totalSamples, discardedPackets: this.discardedPackets, discardedSamples: this.discardedSamples,
      missingReason: this.missingReason, entries: this.rows.map(row => ({...row}))}
  }
  private time(atMs: number): boolean {
    if (!Number.isFinite(atMs) || atMs < this.lastAtMs) {this.missingReason = 'receipt_clock_invalid'; return false}
    this.lastAtMs = atMs; return true
  }
  private emit(): void {this.report(this.snapshot())}
}
