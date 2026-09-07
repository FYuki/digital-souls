import {PostGainOutputArchive, type OutputArchiveSnapshot} from './post-gain-archive'
import {PostGainOutputAudit, type GainAuditMessage, type GainAuditSnapshot} from './post-gain-audit'

export type StaleAudioObservation = Readonly<{
  responseId: string; sessionId: string; generation: number; observedAtMs: number
  receivedAfterCancelPackets: number; receivedAfterCancelSamples: number
  receiveBoundary: 'decoded_packet_delivered'; cancelBoundary: 'client_cancel_confirmed'
  outputContext: Readonly<{sampleRate: number | null; baseLatencySeconds: number | null; outputLatencySeconds: number | null}>
  outputArchive: OutputArchiveSnapshot
  graphClosed: boolean; audit: GainAuditSnapshot
}>

// 未対応・不正な測定値を0秒として扱わない。
function measuredNonnegative(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

// 明示診断時だけ追加する。音声はgain後段をそのまま通し、停止処理から独立してdrainを観測する。
export class PostGainAudioMonitor {
  readonly node: AudioWorkletNode
  private readonly archive = new PostGainOutputArchive()
  private readonly tracker = new PostGainOutputAudit()
  private readonly timer: ReturnType<typeof setInterval>
  private timeout: ReturnType<typeof setTimeout> | null = null
  private finishRequested = false
  private closed = false
  private cancelled = false
  private packets = 0
  private samples = 0
  private readonly drained: Promise<void>
  private settle: () => void = () => undefined

  constructor(private readonly context: AudioContext, private readonly responseId: string,
    private readonly sessionId: string, private readonly generation: number,
    private readonly report: (row: StaleAudioObservation) => void) {
    this.drained = new Promise(resolve => {this.settle = resolve})
    this.node = new AudioWorkletNode(context, 'post-gain-audit', {
      numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 1, channelCountMode: 'explicit',
    })
    this.node.connect(context.destination)
    this.node.port.onmessage = (event: MessageEvent<GainAuditMessage>) => {
      if (this.closed) return
      this.archive.record({kind: 'message', message: event.data, atMs: performance.now()})
      this.tracker.record(event.data)
      this.poll()
    }
    this.timer = setInterval(() => this.poll(), 5)
  }

  cancel(atMs: number): void {
    if (this.closed || this.cancelled) return
    this.cancelled = true
    this.archive.lock(atMs)
    this.tracker.markCancelled({lowerMs: atMs, upperMs: atMs}, performance.now())
    this.finish()
    this.emit()
  }

  received(sampleCount: number): void {
    if (this.closed || !this.cancelled) return
    if (!Number.isSafeInteger(sampleCount) || sampleCount < 0) {
      this.tracker.record({kind: 'missing', reason: 'audit_sample_invalid'})
    } else {this.packets++; this.samples += sampleCount}
    this.emit()
  }

  async dispose(): Promise<void> {
    this.finish()
    await this.drained
    if (this.closed) return
    this.closed = true
    clearInterval(this.timer)
    if (this.timeout !== null) clearTimeout(this.timeout)
    this.tracker.close()
    this.archive.close(performance.now())
    this.node.disconnect()
    this.node.port.close()
    this.emit()
  }

  private finish(): void {
    if (this.finishRequested || this.closed) return
    this.finishRequested = true
    this.node.port.postMessage({kind: 'finish'})
    this.timeout = setTimeout(() => {
      this.tracker.close()
      clearInterval(this.timer)
      this.settle()
      this.emit()
    }, 2000)
  }

  private poll(): void {
    if (this.closed) return
    const timestamp = this.context.getOutputTimestamp(), atMs = performance.now()
    this.archive.record({kind: 'clock', timestamp, sampleRate: this.context.sampleRate, atMs})
    this.tracker.poll(timestamp, this.context.sampleRate, atMs)
    const row = this.tracker.snapshot()
    if (row.drained || row.missingReason !== null) {
      clearInterval(this.timer)
      if (this.timeout !== null) {clearTimeout(this.timeout); this.timeout = null}
      this.settle()
      this.emit()
    }
  }

  private emit(): void {
    if (!this.cancelled) return
    const missing = this.tracker.snapshot().missingReason
    if (missing !== null) this.archive.fail(missing)
    this.report({responseId: this.responseId, sessionId: this.sessionId, generation: this.generation,
      observedAtMs: performance.now(), receivedAfterCancelPackets: this.packets, receivedAfterCancelSamples: this.samples,
      receiveBoundary: 'decoded_packet_delivered', cancelBoundary: 'client_cancel_confirmed',
      outputContext: {sampleRate: measuredNonnegative(this.context.sampleRate),
        baseLatencySeconds: measuredNonnegative(this.context.baseLatency),
        outputLatencySeconds: measuredNonnegative(this.context.outputLatency)},
      outputArchive: this.archive.snapshot(), graphClosed: this.closed, audit: this.tracker.snapshot()})
  }
}
