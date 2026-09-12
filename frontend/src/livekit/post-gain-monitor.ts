import {PostGainOutputArchive, type OutputArchiveSnapshot} from './post-gain-archive'
import {PostGainOutputAudit, type GainAuditMessage, type GainAuditSnapshot} from './post-gain-audit'

import type {OutputStopRequest} from './private-contract'

export type OutputStopConfirmation = Readonly<{
  endFrame: number; outputClockPassedFrame: number; observedAtMs: number
}>
export type PostGainWorkletMessage = GainAuditMessage | Readonly<{kind: 'stopped'; endFrame: number}>

export type StaleAudioObservation = Readonly<{
  outputStopConfirmation?: OutputStopConfirmation
  outputGraphId?: string
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

// 通常出力の停止確認を担当する。詳細な履歴の保存は明示診断時だけ行う。
export class PostGainAudioMonitor {
  readonly node: AudioWorkletNode
  private readonly graphId = crypto.randomUUID()
  private stopRequestId: string | null = null
  private readonly archive = new PostGainOutputArchive()
  private readonly tracker = new PostGainOutputAudit()
  private stopTracker: PostGainOutputAudit | null = null
  private readonly timer: ReturnType<typeof setInterval>
  private timeout: ReturnType<typeof setTimeout> | null = null
  private finishRequested = false
  private closed = false
  private cancelled = false
  private packets = 0
  private samples = 0
  private stopPromise: Promise<OutputStopConfirmation> | null = null
  private resolveStop: ((value: OutputStopConfirmation) => void) | null = null
  private rejectStop: ((error: Error) => void) | null = null
  private stopTimeout: ReturnType<typeof setTimeout> | null = null
  private stopFrame: number | null = null
  private stopMarkerInvalid = false
  private stopIssuedAfterFrame: number | null = null
  private lastWorkletOutput: {endFrame: number; nonzeroSamples: number} | null = null
  private stopConfirmation: OutputStopConfirmation | null = null
  private readonly drained: Promise<void>
  private settle: () => void = () => undefined

  constructor(private readonly context: AudioContext, private readonly responseId: string,
    private readonly sessionId: string, private readonly generation: number,
    private readonly report?: (row: StaleAudioObservation) => void) {
    this.drained = new Promise(resolve => {this.settle = resolve})
    this.node = new AudioWorkletNode(context, 'post-gain-audit', {
      numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1], channelCount: 1, channelCountMode: 'explicit',
    })
    this.node.connect(context.destination)
    this.node.port.onmessage = (event: MessageEvent<PostGainWorkletMessage>) => {
      if (this.closed) return
      if (event.data.kind === 'stopped') {
        const endFrame = event.data.endFrame
        if (this.report !== undefined && this.stopRequestId !== null) {
          this.archive.record({kind: 'stop_marker', endFrame, atMs: performance.now()})
        }
        if (this.stopPromise === null || this.stopFrame !== null || !Number.isSafeInteger(endFrame) || endFrame < 0
          || this.lastWorkletOutput?.endFrame !== endFrame || this.lastWorkletOutput.nonzeroSamples !== 0
          || (this.stopIssuedAfterFrame !== null && endFrame <= this.stopIssuedAfterFrame)) {
          this.stopMarkerInvalid = true
          this.failStop('output_stop_marker_invalid')
          return
        }
        this.stopFrame = endFrame
        this.poll()
        return
      }
      if (this.report !== undefined) this.archive.record({kind: 'message', message: event.data, atMs: performance.now()})
      this.tracker.record(event.data)
      this.stopTracker?.record(event.data)
      if (event.data.kind === 'output') {
        const last = event.data.intervals.at(-1)
        if (last !== undefined) this.lastWorkletOutput = {endFrame: last.endFrame, nonzeroSamples: last.nonzeroSamples}
      }
      this.poll()
    }
    this.timer = setInterval(() => this.poll(), 5)
  }

  // 最終出力段を不可逆に無音化し、既に出力待ちだったquantumが実出力時計を通過するまで待つ。
  // timeout・時計欠測・閉鎖を停止成功にはしない。監視はcancel境界の後も継続する。
  stopAndConfirm(request?: OutputStopRequest): Promise<OutputStopConfirmation> {
    if (request !== undefined && (request.sessionId !== this.sessionId || request.responseId !== this.responseId
      || request.generation !== this.generation || (this.stopRequestId !== null && this.stopRequestId !== request.requestId))) {
      return Promise.reject(new Error('output_stop_request_mismatch'))
    }
    if (this.stopPromise !== null) return this.stopPromise
    if (this.closed || this.finishRequested) return Promise.reject(new Error('output_stop_monitor_unavailable'))
    if (this.stopMarkerInvalid) return Promise.reject(new Error('output_stop_marker_invalid'))
    if (request !== undefined) {
      this.stopRequestId = request.requestId
      if (this.report !== undefined) this.archive.record({kind: 'stop_requested', requestId: request.requestId,
        sessionId: this.sessionId, responseId: this.responseId, generation: this.generation,
        graphId: this.graphId, atMs: performance.now()})
    }
    // 全再生の品質欠測と、今回の停止境界の証明を分離する。判定規則は共通。
    this.stopTracker = new PostGainOutputAudit({clockRegression: 'wait'})
    this.stopTracker.poll(this.context.getOutputTimestamp(), this.context.sampleRate, performance.now())
    this.stopIssuedAfterFrame = this.lastWorkletOutput?.endFrame ?? null
    this.stopPromise = new Promise((resolve, reject) => {this.resolveStop = resolve; this.rejectStop = reject})
    this.stopTimeout = setTimeout(() => this.failStop('output_stop_confirmation_timeout'), 1000)
    this.node.port.postMessage({kind: 'stop'})
    return this.stopPromise
  }

  private failStop(reason: string): void {
    if (this.stopTimeout !== null) {clearTimeout(this.stopTimeout); this.stopTimeout = null}
    this.rejectStop?.(new Error(reason))
    this.resolveStop = null; this.rejectStop = null
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
    this.failStop('output_stop_monitor_closed')
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
    if (this.report !== undefined) this.archive.record({kind: 'clock', timestamp, sampleRate: this.context.sampleRate, atMs})
    this.tracker.poll(timestamp, this.context.sampleRate, atMs)
    const row = this.tracker.snapshot()
    this.stopTracker?.poll(timestamp, this.context.sampleRate, atMs)
    const stop = this.stopTracker?.snapshot()
    if (stop && stop.missingReason !== null) this.failStop('output_stop_observation_invalid')
    else if (this.stopFrame !== null && stop && stop.outputClockPassedFrame !== null
      && stop.outputClockPassedFrame >= this.stopFrame && this.resolveStop !== null) {
      this.stopConfirmation = {endFrame: this.stopFrame, outputClockPassedFrame: stop.outputClockPassedFrame, observedAtMs: atMs}
      if (this.report !== undefined && this.stopRequestId !== null) {
        this.archive.record({kind: 'stop_confirmed', requestId: this.stopRequestId, endFrame: this.stopFrame,
          outputClockPassedFrame: stop.outputClockPassedFrame, atMs})
      }
      this.resolveStop(this.stopConfirmation)
      this.resolveStop = null; this.rejectStop = null
      if (this.stopTimeout !== null) {clearTimeout(this.stopTimeout); this.stopTimeout = null}
    }
    if (this.finishRequested && (row.drained || row.missingReason !== null)) {
      clearInterval(this.timer)
      if (this.timeout !== null) {clearTimeout(this.timeout); this.timeout = null}
      this.settle()
      this.emit()
    }
  }

  private emit(): void {
    if (!this.cancelled || this.report === undefined) return
    const missing = this.tracker.snapshot().missingReason
    if (missing !== null) this.archive.fail(missing)
    this.report({...(this.stopConfirmation === null ? {} : {outputStopConfirmation: {...this.stopConfirmation}}), responseId: this.responseId, sessionId: this.sessionId, generation: this.generation,
      outputGraphId: this.graphId, observedAtMs: performance.now(), receivedAfterCancelPackets: this.packets, receivedAfterCancelSamples: this.samples,
      receiveBoundary: 'decoded_packet_delivered', cancelBoundary: 'client_cancel_confirmed',
      outputContext: {sampleRate: measuredNonnegative(this.context.sampleRate),
        baseLatencySeconds: measuredNonnegative(this.context.baseLatency),
        outputLatencySeconds: measuredNonnegative(this.context.outputLatency)},
      outputArchive: this.archive.snapshot(), graphClosed: this.closed, audit: this.tracker.snapshot()})
  }
}
