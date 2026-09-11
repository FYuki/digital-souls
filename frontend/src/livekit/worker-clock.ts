export type ClockBounds = Readonly<{ lowerMs: number; upperMs: number }>

const finiteTime = (value: number) => Number.isFinite(value) && value >= 0

// timeOriginのepoch換算に依存せず、main送信→worker応答生成→main受信でoffsetを囲む。
// 対象Chromiumの100us時計に対し、2時計の差分へ両端各200usの丸め余裕を残す。
export class WorkerClockCalibration {
  private lowerMs = -Infinity
  private upperMs = Infinity
  private samples = 0
  private invalid = false

  add(sentAtMs: number, workerAtMs: number, receivedAtMs: number): void {
    if (this.invalid || this.samples >= 10 || ![sentAtMs, workerAtMs, receivedAtMs].every(finiteTime)
      || sentAtMs > receivedAtMs) {
      this.invalid = true
      return
    }
    this.lowerMs = Math.max(this.lowerMs, sentAtMs - workerAtMs - 0.2)
    this.upperMs = Math.min(this.upperMs, receivedAtMs - workerAtMs + 0.2)
    this.samples += 1
    if (this.lowerMs > this.upperMs) this.invalid = true
  }

  bounds(): ClockBounds | undefined {
    if (this.invalid || this.samples !== 10 || this.upperMs - this.lowerMs > 1) return undefined
    return { lowerMs: this.lowerMs, upperMs: this.upperMs }
  }

  toMain(workerAtMs: number): ClockBounds | undefined {
    const offset = this.bounds()
    if (offset === undefined || !finiteTime(workerAtMs)) return undefined
    const lowerMs = offset.lowerMs + workerAtMs
    const upperMs = offset.upperMs + workerAtMs
    if (!Number.isFinite(lowerMs + upperMs) || lowerMs < 0) return undefined
    return { lowerMs, upperMs }
  }
}
