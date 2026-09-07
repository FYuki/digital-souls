import {renderQuantumClockSource} from './render-quantum-clock'

// 応答ごとのGainNodeの後段で観測する。rendererを止めても監視は止めない。
// 波形は出力へそのまま通し、観測にはsample数と非ゼロ区間の端だけを渡す。
export const postGainAuditSource = `{
${renderQuantumClockSource}
class PostGainAudit extends AudioWorkletProcessor {
  constructor() {
    super(); this.clock = new RenderQuantumClock(); this.pending = [];
    this.finishing = false; this.finished = false; this.failed = false;
    this.port.onmessage = ({data}) => {if (data.kind === 'finish') this.finishing = true;};
  }
  fail(reason) {
    if (!this.failed) this.port.postMessage({kind: 'missing', reason});
    this.failed = true; this.pending = [];
  }
  process(inputs, outputs) {
    const out = outputs[0]?.[0], input = inputs[0]?.[0];
    if (!out) {this.fail('audit_output_unavailable'); return true;}
    out.fill(0);
    if (input) out.set(input.subarray(0, out.length));
    if (outputs.length !== 1 || outputs[0].length !== 1 || inputs.length > 1
      || (inputs[0]?.length ?? 0) > 1 || (input && input.length !== out.length)) {
      this.fail('audit_channel_mismatch'); return true;
    }
    let nonzeroSamples = 0, firstNonzeroFrame = null, lastNonzeroFrame = null;
    for (let i = 0; i < out.length; i++) {
      if (!Number.isFinite(out[i])) {this.fail('audit_sample_invalid'); return true;}
      if (out[i] !== 0) {nonzeroSamples++; firstNonzeroFrame ??= i; lastNonzeroFrame = i;}
    }
    if (this.finished) {
      if (nonzeroSamples > 0) this.fail('audit_output_after_finish');
      return true;
    }
    let quantum;
    try {quantum = this.clock.read(currentFrame, out.length);}
    catch {this.fail('audit_render_clock_unreconciled'); return true;}
    if (!quantum) {
      if (nonzeroSamples > 0) this.fail('audit_initial_output_unmapped');
      return true;
    }
    if (this.failed) return true;
    this.pending.push({startFrame: quantum.frame, endFrame: quantum.frame + out.length,
      nonzeroSamples, firstNonzeroFrame: firstNonzeroFrame === null ? null : quantum.frame + firstNonzeroFrame,
      lastNonzeroFrame: lastNonzeroFrame === null ? null : quantum.frame + lastNonzeroFrame});
    if (this.pending.length > 500) {this.fail('audit_observation_overflow'); return true;}
    if (quantum.confirmed && (this.pending.length >= 16 || this.finishing)) {
      this.port.postMessage({kind: 'output', intervals: this.pending,
        confirmedFrame: quantum.frame}); this.pending = [];
      if (this.finishing) {
        this.port.postMessage({kind: 'finished', endFrame: quantum.frame + out.length});
        this.finished = true;
      }
    }
    return true;
  }
}
registerProcessor('post-gain-audit', PostGainAudit);
}
`

export type GainAuditInterval = Readonly<{
  startFrame: number; endFrame: number; nonzeroSamples: number
  firstNonzeroFrame: number | null; lastNonzeroFrame: number | null
}>

export type GainAuditMessage = Readonly<{
  kind: 'output'; intervals: readonly GainAuditInterval[]; confirmedFrame: number
}> | Readonly<{kind: 'finished'; endFrame: number}>
  | Readonly<{kind: 'missing'; reason: GainAuditMissingReason}>

export type GainAuditMissingReason = 'audit_output_unavailable' | 'audit_channel_mismatch'
  | 'audit_sample_invalid' | 'audit_render_clock_unreconciled' | 'audit_initial_output_unmapped'
  | 'audit_observation_overflow' | 'audit_interval_invalid' | 'audit_output_clock_invalid'
  | 'audit_output_gap' | 'audit_finish_invalid' | 'audit_cancel_clock_invalid'
  | 'audit_closed_before_drain' | 'audit_output_after_finish'

type Bounds = Readonly<{lowerMs: number; upperMs: number}>
const time = (n: number): boolean => Number.isFinite(n) && n >= 0
const frame = (n: number): boolean => Number.isSafeInteger(n) && n >= 0

export type GainAuditSnapshot = Readonly<{
  complete: boolean; drained: boolean; missingReason: GainAuditMissingReason | null
  cancelBoundsMs: Bounds | null; outputClockPassedFrame: number | null
  firstOutputAtMs: number | null; lastOutputEndAtMs: number | null
  clockFailure: Readonly<{stage: 'invalid_timestamp' | 'timestamp_regression' | 'negative_output_time' | 'mapped_interval_regression'; values: Readonly<Record<string, number>>}> | null
  nonzeroSamplesAfterCancelLower: number; nonzeroSamplesAfterCancelUpper: number
  boundaryUncertainIntervals: number; observedIntervals: number
  // 音響スピーカーではなく、独立したpost-gain graphのbrowser出力時計が境界。
  observationBoundary: 'post_gain_browser_output_clock'
}>

// cancel時点の未出力区間を捨てない。finish通知後も出力時計通過までpollを続ける。
export class PostGainOutputAudit {
  private pending: GainAuditInterval[] = []
  private cancel: Bounds | null = null
  private lastRecorded: number | null = null
  private firstRecorded: number | null = null
  private passed: number | null = null
  private finished: number | null = null
  private missing: GainAuditMissingReason | null = null
  private lower = 0
  private upper = 0
  private uncertain = 0
  private count = 0
  private firstOutputAt: number | null = null
  private lastOutputEndAt: number | null = null
  private clockFailure: GainAuditSnapshot['clockFailure'] = null
  private lastClock: {contextTime: number; performanceTime: number; observedAtMs: number} | null = null

  markCancelled(bounds: Bounds, observedAtMs: number): void {
    if (this.cancel !== null && bounds.lowerMs === this.cancel.lowerMs && bounds.upperMs === this.cancel.upperMs) return
    if (!time(bounds.lowerMs) || !time(bounds.upperMs) || bounds.lowerMs > bounds.upperMs
      || !time(observedAtMs) || bounds.upperMs > observedAtMs
      // 過去に集計済みの出力より古いcancelを後付けしてゼロへ補完しない。
      || (this.lastClock !== null && bounds.lowerMs < this.lastClock.observedAtMs)
      || (this.cancel !== null && (bounds.lowerMs !== this.cancel.lowerMs || bounds.upperMs !== this.cancel.upperMs))) {
      this.fail('audit_cancel_clock_invalid'); return;
    }
    this.cancel ??= {...bounds}
  }

  record(message: GainAuditMessage): void {
    if (this.missing !== null) return
    if (message.kind === 'missing') {this.fail(message.reason); return}
    if (message.kind === 'finished') {
      if (!frame(message.endFrame) || message.endFrame !== this.lastRecorded || this.finished !== null) {
        this.fail('audit_finish_invalid'); return;
      }
      this.finished = message.endFrame; return
    }
    if (this.finished !== null || !frame(message.confirmedFrame) || !message.intervals.length) {
      this.fail('audit_interval_invalid'); return;
    }
    for (const row of message.intervals) {
      const n = row.endFrame - row.startFrame
      if (!frame(row.startFrame) || !frame(row.endFrame) || n < 1 || n > 2048
        || row.startFrame > message.confirmedFrame || !frame(row.nonzeroSamples) || row.nonzeroSamples > n
        || (row.nonzeroSamples === 0 ? row.firstNonzeroFrame !== null || row.lastNonzeroFrame !== null
          : row.firstNonzeroFrame === null || row.lastNonzeroFrame === null
            || !frame(row.firstNonzeroFrame) || !frame(row.lastNonzeroFrame)
            || row.firstNonzeroFrame < row.startFrame || row.lastNonzeroFrame >= row.endFrame
            || row.lastNonzeroFrame < row.firstNonzeroFrame
            || row.nonzeroSamples > row.lastNonzeroFrame - row.firstNonzeroFrame + 1
            || (row.nonzeroSamples === 1 && row.firstNonzeroFrame !== row.lastNonzeroFrame))) {
        this.fail('audit_interval_invalid'); return;
      }
      if (this.lastRecorded !== null && row.startFrame !== this.lastRecorded) {
        this.fail('audit_output_gap'); return;
      }
      if (this.pending.length >= 1000) {this.fail('audit_observation_overflow'); return}
      this.firstRecorded ??= row.startFrame
      this.lastRecorded = row.endFrame; this.pending.push({...row})
    }
  }

  poll(timestamp: AudioTimestamp, sampleRate: number, observedAtMs: number): void {
    if (this.missing !== null) return
    const {contextTime, performanceTime} = timestamp
    if (sampleRate !== 48000 || contextTime === undefined || performanceTime === undefined
      || !time(contextTime) || !time(performanceTime) || !time(observedAtMs)) {
      this.clockFailure = {stage: 'invalid_timestamp', values: {}}
      this.fail('audit_output_clock_invalid'); return;
    }
    // 初期化前の全ゼロ時計には観測の証明力がない。
    if (contextTime === 0 || performanceTime === 0) return
    if (this.lastClock !== null && (contextTime < this.lastClock.contextTime
      || performanceTime < this.lastClock.performanceTime || observedAtMs < this.lastClock.observedAtMs)) {
      this.clockFailure = {stage: 'timestamp_regression', values: {contextTime, performanceTime, observedAtMs,
        previousContextTime: this.lastClock.contextTime, previousPerformanceTime: this.lastClock.performanceTime,
        previousObservedAtMs: this.lastClock.observedAtMs}}
      this.fail('audit_output_clock_invalid'); return;
    }
    this.lastClock = {contextTime, performanceTime, observedAtMs}
    const passedFrame = Math.floor(contextTime * sampleRate)
    while (this.pending.length && this.pending[0].endFrame <= passedFrame) {
      const row = this.pending[0]
      const at = (f: number) => performanceTime + (f / sampleRate - contextTime) * 1000
      if (at(row.endFrame) > observedAtMs) break
      if (at(row.startFrame) < 0) {
        this.clockFailure = {stage: 'negative_output_time', values: {contextTime, performanceTime, observedAtMs, startFrame: row.startFrame}}
        this.fail('audit_output_clock_invalid'); return
      }
      if (this.lastOutputEndAt !== null && at(row.startFrame) < this.lastOutputEndAt - 0.001) {
        this.clockFailure = {stage: 'mapped_interval_regression', values: {contextTime, performanceTime, observedAtMs,
          previousOutputEndAtMs: this.lastOutputEndAt, mappedStartAtMs: at(row.startFrame), startFrame: row.startFrame,
          deltaMs: at(row.startFrame) - this.lastOutputEndAt}}
        this.fail('audit_output_clock_invalid'); return;
      }
      this.pending.shift(); this.passed = row.endFrame; this.count++
      this.firstOutputAt ??= at(row.startFrame)
      this.lastOutputEndAt = at(row.endFrame)
      if (this.cancel !== null && row.nonzeroSamples > 0) {
        const first = row.firstNonzeroFrame!, last = row.lastNonzeroFrame!
        // sample区間が境界をまたぐ場合も提示の可能性を残す。浮動小数点の
        // 丸めで境界sampleをゼロへ落とさず、上下限が一致しない場合は未確定にする。
        const beforeUpper = Math.max(0, Math.min(last - first + 1,
          Math.ceil((this.cancel.upperMs - at(first)) * 48 + 1e-6)))
        const afterLower = Math.max(0, Math.min(last - first + 1,
          last - first + 1 - Math.floor((this.cancel.lowerMs - at(first)) * 48 - 1e-6)))
        const lower = Math.max(0, row.nonzeroSamples - beforeUpper)
        const upper = Math.min(row.nonzeroSamples, afterLower)
        this.lower += lower; this.upper += upper
        if (lower !== upper) this.uncertain++
      }
    }
  }

  close(): void {
    if (!this.snapshot().drained) this.fail('audit_closed_before_drain')
  }

  snapshot(): GainAuditSnapshot {
    const drained = this.missing === null && this.finished !== null
      && this.firstRecorded !== null && this.passed === this.finished && this.pending.length === 0
    return {drained, complete: drained && this.cancel !== null
      && this.firstOutputAt !== null && this.lastOutputEndAt !== null
      && this.firstOutputAt <= this.cancel.lowerMs && this.lastOutputEndAt > this.cancel.upperMs,
    missingReason: this.missing, cancelBoundsMs: this.cancel === null ? null : {...this.cancel},
    outputClockPassedFrame: this.passed, nonzeroSamplesAfterCancelLower: this.lower,
    firstOutputAtMs: this.firstOutputAt, lastOutputEndAtMs: this.lastOutputEndAt,
    clockFailure: this.clockFailure === null ? null : {stage: this.clockFailure.stage, values: {...this.clockFailure.values}},
    nonzeroSamplesAfterCancelUpper: this.upper, boundaryUncertainIntervals: this.uncertain,
    observedIntervals: this.count, observationBoundary: 'post_gain_browser_output_clock'}
  }

  private fail(reason: GainAuditMissingReason): void {this.missing ??= reason; this.pending = []}
}
