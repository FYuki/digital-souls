import type {RetryTimer} from './control'

// 受理済みCore eventのACKだけを再送する。本文を保持せず、受信側の適用は繰り返さない。
export class CoreAckOutbox {
  private readonly pending = new Map<string, number>()
  private timerHandle: ReturnType<typeof setTimeout> | null = null
  private flushing = false
  private version = 0

  constructor(private readonly send: (eventId: string) => Promise<void>, private readonly timer: RetryTimer,
    private readonly expired: () => void, private readonly deferred: () => void = () => undefined) {}

  enqueue(eventId: string): void {
    if (!this.pending.has(eventId)) {
      if (this.pending.size >= 256) throw new Error('Core ACK capacity exceeded')
      const now = this.timer.now()
      if (!Number.isFinite(now) || now < 0) throw new Error('Core ACK clock invalid')
      this.pending.set(eventId, now)
    }
    void this.flush()
  }

  clear(): void {
    this.version++
    this.pending.clear()
    this.flushing = false
    if (this.timerHandle !== null) this.timer.cancel(this.timerHandle)
    this.timerHandle = null
  }

  private async flush(): Promise<void> {
    if (this.flushing || !this.pending.size) return
    if (this.timerHandle !== null) this.timer.cancel(this.timerHandle)
    this.timerHandle = null
    const version = this.version
    this.flushing = true
    try {
      for (const [eventId, atMs] of this.pending) {
        const now = this.timer.now()
        if (!Number.isFinite(now) || now < atMs || now - atMs >= 60000) {
          this.clear(); this.expired(); return
        }
        try {await this.send(eventId)}
        catch {
          if (version === this.version) this.deferred()
          return
        }
        if (version !== this.version) return
        this.pending.delete(eventId)
      }
    } finally {
      if (version === this.version) {
        this.flushing = false
        if (this.pending.size) this.timerHandle = this.timer.schedule(() => {
          this.timerHandle = null
          void this.flush()
        }, 250)
      }
    }
  }
}
