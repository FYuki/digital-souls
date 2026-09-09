// 較正要求の送信と応答の受信でremote時計を囲む。epochの一致は仮定しない。
export type TimeBounds = Readonly<{lowerMs: number; upperMs: number}>
export type ClockSample = Readonly<{sentAtMs: number; remoteAtMs: number; receivedAtMs: number}>
const validTime = (value: number) => Number.isFinite(value) && value >= 0
const validBounds = (bounds: TimeBounds) => Number.isFinite(bounds.lowerMs) && Number.isFinite(bounds.upperMs)
  && bounds.lowerMs <= bounds.upperMs

export function clockOffset(samples: readonly ClockSample[]): TimeBounds {
  if (samples.length < 5) throw new Error('clock samples missing')
  let lowerMs = -Infinity, upperMs = Infinity
  for (const {sentAtMs, remoteAtMs, receivedAtMs} of samples) {
    if (![sentAtMs, remoteAtMs, receivedAtMs].every(validTime) || sentAtMs > receivedAtMs) {
      throw new Error('invalid causal clock sample')
    }
    // Browserの100us量子化と2時計の差分に両端200usの余裕を確保する。
    lowerMs = Math.max(lowerMs, sentAtMs - remoteAtMs - 0.2)
    upperMs = Math.min(upperMs, receivedAtMs - remoteAtMs + 0.2)
  }
  const bounds = {lowerMs, upperMs}
  if (!validBounds(bounds)) throw new Error('clock offset inconsistent')
  return bounds
}

export type FaultClockCalibration = Readonly<{
  browser: readonly ClockSample[]; faultRunner: readonly ClockSample[]
}>

// before/afterの全sampleが同じoffsetで説明できなければ、時計の変更・driftとして失敗する。
export function faultToBrowserOffset(before: FaultClockCalibration, after: FaultClockCalibration): TimeBounds {
  for (const calibration of [before, after]) {
    clockOffset(calibration.browser); clockOffset(calibration.faultRunner)
  }
  const browser = clockOffset([...before.browser, ...after.browser])
  const fault = clockOffset([...before.faultRunner, ...after.faultRunner])
  const bounds = {lowerMs: fault.lowerMs - browser.upperMs, upperMs: fault.upperMs - browser.lowerMs}
  if (!validBounds(bounds) || bounds.upperMs - bounds.lowerMs > 20) throw new Error('fault clock uncertainty exceeds 20ms')
  return bounds
}

export function faultTimeInBrowser(timestampNs: string, offset: TimeBounds): TimeBounds {
  if (!/^(0|[1-9][0-9]*)$/.test(timestampNs) || !validBounds(offset)
    || offset.upperMs - offset.lowerMs > 20) throw new Error('invalid fault clock mapping')
  const ns = BigInt(timestampNs)
  const ms = Number(ns / 1000000n) + Number(ns % 1000000n) / 1e6
  const result = {lowerMs: ms + offset.lowerMs, upperMs: ms + offset.upperMs}
  if (!Number.isSafeInteger(Number(ns / 1000000n)) || !validBounds(result) || result.lowerMs < 0) {
    throw new Error('fault timestamp outside browser clock')
  }
  return result
}

// 復旧前に送った要求の遅着ackは除外し、遅延は復旧時刻の下限から保守的に算出する。
export function recoveryLatencyUpperMs(restored: TimeBounds, sentAtMs: number, receivedAtMs: number): number {
  if (!validBounds(restored) || restored.lowerMs < 0
    || ![sentAtMs, receivedAtMs].every(validTime) || sentAtMs <= restored.upperMs || receivedAtMs < sentAtMs) {
    throw new Error('recovery observation is not causally after restoration')
  }
  return receivedAtMs - restored.lowerMs
}
