import {renderQuantumClockSource} from './render-quantum-clock'

// 応答ごとのGainNodeの後段で観測する。rendererを止めても監視は止めない。
// 波形は出力へそのまま通し、観測にはsample数と非ゼロ区間の端だけを渡す。
export const postGainAuditSource = `{
${renderQuantumClockSource}
class PostGainAudit extends AudioWorkletProcessor {
  constructor() {
    super(); this.clock = new RenderQuantumClock(); this.pending = [];
    this.finishing = false; this.finished = false; this.failed = false;
    this.stopping = false; this.stopAcknowledged = false;
    this.port.onmessage = ({data}) => {
      if (data.kind === 'finish') this.finishing = true;
      if (data.kind === 'stop') this.stopping = true;
    };
  }
  fail(reason) {
    if (!this.failed) this.port.postMessage({kind: 'missing', reason});
    this.failed = true; this.pending = [];
  }
  process(inputs, outputs) {
    const out = outputs[0]?.[0], input = inputs[0]?.[0];
    if (!out) {this.fail('audit_output_unavailable'); return true;}
    out.fill(0);
    if (input && !this.stopping) out.set(input.subarray(0, out.length));
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
    const acknowledgeStop = this.stopping && !this.stopAcknowledged;
    if (quantum.confirmed && (this.pending.length >= 16 || this.finishing || acknowledgeStop)) {
      this.port.postMessage({kind: 'output', intervals: this.pending,
        confirmedFrame: quantum.frame}); this.pending = [];
      if (acknowledgeStop) {
        this.port.postMessage({kind: 'stopped', endFrame: quantum.frame + out.length});
        this.stopAcknowledged = true;
      }
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
  | 'audit_cancel_anchor_missing' | 'audit_cancel_window_unobserved'

type Bounds = Readonly<{lowerMs: number; upperMs: number}>
const time = (n: number): boolean => Number.isFinite(n) && n >= 0
const frame = (n: number): boolean => Number.isSafeInteger(n) && n >= 0

export type GainAuditSnapshot = Readonly<{
  complete: boolean; drained: boolean; missingReason: GainAuditMissingReason | null
  cancelBoundsMs: Bounds | null; outputClockPassedFrame: number | null
  cancelOutputFrameBounds: Readonly<{lowerFrame: number; upperFrame: number}> | null
  clockCorrelationMethod: 'bracketing_output_timestamps' | 'bracketing_output_timestamps_with_confirmed_stop'
  timestampRoundingMarginMs: number
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
  private lastOutputPoint: {frame: number; atMs: number} | null = null
  private cancelPending: GainAuditInterval[] = []
  private cancelLowerFrame: number | null = null
  private confirmedOutputFrameFloor: number | null = null
  private cancelUpperFrame: number | null = null
  // このChromium診断のperformance時計丸めを上下限へ含める。外挿の誤差許容値ではない。
  private readonly roundingMarginMs = 0.2
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

  constructor(private readonly options: {clockRegression?: 'fail' | 'wait'} = {}) {}

  markCancelled(bounds: Bounds, observedAtMs: number, confirmedOutputFrameFloor?: number): void {
    const floor = confirmedOutputFrameFloor ?? null
    if (this.cancel !== null && bounds.lowerMs === this.cancel.lowerMs && bounds.upperMs === this.cancel.upperMs
      && floor === this.confirmedOutputFrameFloor) return
    if (!time(bounds.lowerMs) || !time(bounds.upperMs) || bounds.lowerMs > bounds.upperMs
      || !time(observedAtMs) || bounds.upperMs > observedAtMs
      || (floor !== null && !frame(floor))
      // 過去に集計済みの出力より古いcancelを後付けしてゼロへ補完しない。
      || (this.lastClock !== null && bounds.lowerMs < this.lastClock.observedAtMs)
      || (this.cancel !== null && (bounds.lowerMs !== this.cancel.lowerMs || bounds.upperMs !== this.cancel.upperMs
        || floor !== this.confirmedOutputFrameFloor))) {
      this.fail('audit_cancel_clock_invalid'); return;
    }
    this.cancel ??= {...bounds}
    this.confirmedOutputFrameFloor = floor
    const before = this.lastOutputPoint
    if (!before || before.atMs + this.roundingMarginMs > bounds.lowerMs) {
      this.fail('audit_cancel_anchor_missing'); return
    }
    // floorは独立replayで要求・marker・時計通過・server確認順序を検証した場合だけ渡す。
    // 取消以前に出力済みと証明したframeを、時計probeの粗い下限へ戻さない。
    this.cancelLowerFrame = Math.max(0, Math.floor(before.frame) - 1, floor ?? 0)
    // 採用済みtimestampは観測時刻以前、cancelは直前poll以後を要求する。
    // 既に確認した区間がlowerを越えていたら、後付けの境界として拒否する。
    if (this.passed !== null && this.passed > this.cancelLowerFrame) this.fail('audit_cancel_clock_invalid')

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
    // 未観測の未来timestampは比較の基準にも保存しない。後続の実観測を、
    // 採用していない未来値との逆行として誤って欠測にしない。
    if (performanceTime + this.roundingMarginMs > observedAtMs) return
    if (this.lastClock !== null && (contextTime < this.lastClock.contextTime
      || performanceTime < this.lastClock.performanceTime || observedAtMs < this.lastClock.observedAtMs)) {
      // 停止専用の確認では逆行値を採用せず、既存anchorを保持して次の実測を待つ。
      // 全再生の品質監査は従来どおり欠測とする。
      if (this.options.clockRegression === 'wait') return
      this.clockFailure = {stage: 'timestamp_regression', values: {contextTime, performanceTime, observedAtMs,
        previousContextTime: this.lastClock.contextTime, previousPerformanceTime: this.lastClock.performanceTime,
        previousObservedAtMs: this.lastClock.observedAtMs}}
      this.fail('audit_output_clock_invalid'); return;
    }
    this.lastClock = {contextTime, performanceTime, observedAtMs}
    const point = {frame: contextTime * sampleRate, atMs: performanceTime}
    this.lastOutputPoint = point
    if (this.cancel !== null && this.cancelUpperFrame === null
      && point.atMs - this.roundingMarginMs >= this.cancel.upperMs) {
      this.cancelUpperFrame = Math.ceil(point.frame) + 1
      if (this.cancelLowerFrame === null || this.cancelUpperFrame < this.cancelLowerFrame) {
        this.fail('audit_cancel_anchor_missing'); return
      }
      for (const row of this.cancelPending) this.classify(row)
      this.cancelPending = []
    }
    const passedFrame = Math.max(0, Math.floor(point.frame) - 1)
    while (this.pending.length && this.pending[0].endFrame <= passedFrame) {
      const row = this.pending.shift()!
      this.passed = row.endFrame; this.count++
      // 表示用の推定時刻。境界の成否やstale件数の判定には使用しない。
      this.firstOutputAt ??= performanceTime + (row.startFrame / sampleRate - contextTime) * 1000
      this.lastOutputEndAt = performanceTime + (row.endFrame / sampleRate - contextTime) * 1000
      if (this.cancel === null) continue
      if (this.cancelUpperFrame !== null) this.classify(row)
      else {
        if (this.cancelPending.length >= 1000) {this.fail('audit_observation_overflow'); return}
        this.cancelPending.push(row)
      }
    }
    if (this.cancel !== null && this.finished !== null && this.passed === this.finished
      && (this.cancelLowerFrame === null || this.cancelUpperFrame === null || this.firstRecorded === null
        || this.firstRecorded > this.cancelLowerFrame || this.finished <= this.cancelUpperFrame)) {
      this.fail('audit_cancel_window_unobserved')
    }
  }

  private classify(row: GainAuditInterval): void {
    if (row.nonzeroSamples === 0 || this.cancelLowerFrame === null || this.cancelUpperFrame === null) return
    const first = row.firstNonzeroFrame!, last = row.lastNonzeroFrame!, span = last - first + 1
    // lower以後とupper以後を別々に数える。境界をはさむsampleは0へ確定しない。
    const beforeUpper = Math.max(0, Math.min(span, this.cancelUpperFrame - first))
    const afterLower = Math.max(0, Math.min(span, last + 1 - this.cancelLowerFrame))
    const lower = Math.max(0, row.nonzeroSamples - beforeUpper)
    const upper = Math.min(row.nonzeroSamples, afterLower)
    this.lower += lower; this.upper += upper
    if (lower !== upper) this.uncertain++
  }

  close(): void {
    if (!this.snapshot().drained) this.fail('audit_closed_before_drain')
  }

  snapshot(): GainAuditSnapshot {
    const drained = this.missing === null && this.finished !== null
      && this.firstRecorded !== null && this.passed === this.finished && this.pending.length === 0
    return {drained, complete: drained && this.cancel !== null
      && this.cancelLowerFrame !== null && this.cancelUpperFrame !== null
      && this.firstRecorded !== null && this.firstRecorded <= this.cancelLowerFrame
      && this.finished !== null && this.finished > this.cancelUpperFrame,
    cancelOutputFrameBounds: this.cancelLowerFrame === null || this.cancelUpperFrame === null ? null
      : {lowerFrame: this.cancelLowerFrame, upperFrame: this.cancelUpperFrame},
    clockCorrelationMethod: this.confirmedOutputFrameFloor === null ? 'bracketing_output_timestamps'
      : 'bracketing_output_timestamps_with_confirmed_stop', timestampRoundingMarginMs: this.roundingMarginMs,
    missingReason: this.missing, cancelBoundsMs: this.cancel === null ? null : {...this.cancel},
    outputClockPassedFrame: this.passed, nonzeroSamplesAfterCancelLower: this.lower,
    firstOutputAtMs: this.firstOutputAt, lastOutputEndAtMs: this.lastOutputEndAt,
    clockFailure: this.clockFailure === null ? null : {stage: this.clockFailure.stage, values: {...this.clockFailure.values}},
    nonzeroSamplesAfterCancelUpper: this.upper, boundaryUncertainIntervals: this.uncertain,
    observedIntervals: this.count, observationBoundary: 'post_gain_browser_output_clock'}
  }

  private fail(reason: GainAuditMissingReason): void {
    this.missing ??= reason; this.pending = []; this.cancelPending = []
  }
}
