import type {RetryTimer} from './control'

// reliable publishの完了は相手の状態同期完了ではない。同じ要求世代を再送する。
export class StateSyncRequest {
  private handle: ReturnType<typeof setTimeout> | null = null
  private stopped = false
  private sending = false
  private readonly startedAt: number

  constructor(private readonly send: () => Promise<void>, private readonly timer: RetryTimer,
    private readonly expired: () => void) {
    this.startedAt = timer.now()
  }

  start(): void {this.tick()}

  close(): void {
    this.stopped = true
    if (this.handle !== null) this.timer.cancel(this.handle)
    this.handle = null
  }

  private tick(): void {
    if (this.stopped) return
    const now = this.timer.now()
    if (!Number.isFinite(this.startedAt) || this.startedAt < 0 || !Number.isFinite(now)
      || now < this.startedAt || now - this.startedAt >= 60000) {
      this.close(); this.expired(); return
    }
    this.handle = this.timer.schedule(() => {this.handle = null; this.tick()}, 250)
    if (this.sending) return
    this.sending = true
    // 送信Promiseが完了しない場合も上の時計で期限切れを検出する。
    void Promise.resolve().then(() => this.stopped ? undefined : this.send())
      .finally(() => {this.sending = false}).catch(() => undefined)
  }
}
