type ProbeTimer<Handle> = Readonly<{
  now: () => number
  schedule: (callback: () => void, delayMs: number) => Handle
  cancel: (handle: Handle) => void
}>

export type ControlProbeObservation = Readonly<{
  status: 'received' | 'timeout' | 'busy' | 'interrupted' | 'send_failed' | 'clock_invalid' | 'unavailable'
  generation: number
  probeId: string | null
  sentAtMs: number | null
  receivedAtMs: number | null
}>

type PendingProbe<Handle> = {
  generation: number
  probeId: string
  sentAtMs: number
  timer: Handle | null
  resolve: (result: ControlProbeObservation) => void
}

// 送信完了や接続フラグを往復成功にしない。未応答は最大1件・500msだけ保持する。
export class ControlProbeTracker<Handle> {
  private pending: PendingProbe<Handle> | null = null

  constructor(private readonly timer: ProbeTimer<Handle>,
    private readonly createId: () => string = () => crypto.randomUUID()) {}

  start(generation: number,
    send: (probeId: string, generation: number) => Promise<void>): Promise<ControlProbeObservation> {
    if (this.pending !== null) return Promise.resolve(this.empty('busy', generation))
    const sentAtMs = this.timer.now()
    if (!Number.isFinite(sentAtMs) || sentAtMs < 0) {
      return Promise.resolve(this.empty('clock_invalid', generation))
    }
    return new Promise(resolve => {
      const pending: PendingProbe<Handle> = {generation, probeId: this.createId(), sentAtMs, timer: null, resolve}
      this.pending = pending
      pending.timer = this.timer.schedule(() => this.finish(pending, 'timeout'), 500)
      try {
        void send(pending.probeId, generation).catch(() => this.finish(pending, 'send_failed'))
      } catch {
        this.finish(pending, 'send_failed')
      }
    })
  }

  acknowledge(probeId: string, generation: number): void {
    const pending = this.pending
    if (pending === null || pending.probeId !== probeId || pending.generation !== generation) return
    const receivedAtMs = this.timer.now()
    if (!Number.isFinite(receivedAtMs) || receivedAtMs < pending.sentAtMs) {
      this.finish(pending, 'clock_invalid')
      return
    }
    if (receivedAtMs - pending.sentAtMs >= 500) {
      this.finish(pending, 'timeout')
      return
    }
    this.finish(pending, 'received', receivedAtMs)
  }

  reset(): void {
    if (this.pending !== null) this.finish(this.pending, 'interrupted')
  }

  private finish(pending: PendingProbe<Handle>, status: ControlProbeObservation['status'],
    receivedAtMs: number | null = null): void {
    if (this.pending !== pending) return
    this.pending = null
    if (pending.timer !== null) this.timer.cancel(pending.timer)
    pending.resolve({status, generation: pending.generation, probeId: pending.probeId,
      sentAtMs: pending.sentAtMs, receivedAtMs})
  }

  private empty(status: ControlProbeObservation['status'], generation: number): ControlProbeObservation {
    return {status, generation, probeId: null, sentAtMs: null, receivedAtMs: null}
  }
}
