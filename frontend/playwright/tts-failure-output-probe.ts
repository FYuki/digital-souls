import type {Page} from '@playwright/test'

export type FailureOutputRow = {
  sampleRate: number
  outputCount: number
  nonzeroBefore: number
  nonzeroAfter: number
  failureFrameUpper: number | null
  zeroStart: number | null
  zeroEnd: number | null
  outputClockFrame: number | null
  missing: string | null
}
declare global {
  interface Window {
    __ttsFailureOutput?: {
      markFailure: () => void
      snapshot: () => FailureOutputRow[]
    }
  }
}

// 既存post-gain監査workletの数値だけを傍受する。音声・停止動作は変更しない。
export async function installFailureOutputProbe(page: Page): Promise<void> {
  await page.addInitScript(() => {
    type Entry = {
      context: AudioContext
      row: FailureOutputRow
      lastEnd: number | null
    }
    const entries: Entry[] = []
    window.__ttsFailureOutput = {
      markFailure: () => {
        for (const entry of entries) {
          if (entry.row.failureFrameUpper !== null) continue
          // 失敗通知時点ですでにrender中のquantumを含む上限。停止遅延をゼロとみなさない。
          entry.row.failureFrameUpper = Math.ceil(entry.context.currentTime * entry.context.sampleRate) + 256
        }
      },
      snapshot: () => entries.map(entry => {
        const timestamp = entry.context.getOutputTimestamp()
        const contextTime = timestamp.contextTime
        const performanceTime = timestamp.performanceTime
        const frame = contextTime === undefined || performanceTime === undefined
          || !Number.isFinite(contextTime) || !Number.isFinite(performanceTime)
          || contextTime <= 0 || performanceTime <= 0 || performanceTime > performance.now() + 0.2
          ? null : Math.floor(contextTime * entry.context.sampleRate)
        return {...entry.row, outputClockFrame: frame}
      }),
    }
    const Original = window.AudioWorkletNode
    window.AudioWorkletNode = new Proxy(Original, {
      construct(target, args, newTarget) {
        const node = Reflect.construct(target, args, newTarget) as AudioWorkletNode
        const [context, name] = args
        if (name !== 'post-gain-audit' || !(context instanceof AudioContext)) return node
        if (entries.length >= 8) throw new Error('failure output probe capacity exceeded')
        const entry: Entry = {context, lastEnd: null, row: {
          sampleRate: context.sampleRate, outputCount: 0, nonzeroBefore: 0, nonzeroAfter: 0,
          failureFrameUpper: null, zeroStart: null, zeroEnd: null, outputClockFrame: null, missing: null,
        }}
        entries.push(entry)
        node.port.addEventListener('message', ({data}) => {
          if (data.kind === 'missing') {entry.row.missing = String(data.reason); return}
          if (data.kind !== 'output') return
          if (!Array.isArray(data.intervals)) {entry.row.missing = 'invalid_intervals'; return}
          for (const interval of data.intervals) {
            const {startFrame, endFrame, nonzeroSamples} = interval
            if (![startFrame, endFrame, nonzeroSamples].every(Number.isSafeInteger)
              || startFrame < 0 || endFrame <= startFrame || endFrame - startFrame > 2048
              || nonzeroSamples < 0 || nonzeroSamples > endFrame - startFrame
              || (entry.lastEnd !== null && entry.lastEnd !== startFrame)) {
              entry.row.missing = 'invalid_or_missing_output'; return
            }
            entry.lastEnd = endFrame
            entry.row.outputCount += 1
            if (entry.row.failureFrameUpper === null || startFrame < entry.row.failureFrameUpper) {
              entry.row.nonzeroBefore += nonzeroSamples
            } else {
              entry.row.nonzeroAfter += nonzeroSamples
              entry.row.zeroStart ??= startFrame
              entry.row.zeroEnd = endFrame
            }
          }
        })
        return node
      },
    })
  })
}

export function hasOneSecondOfObservedSilence(rows: FailureOutputRow[]): boolean {
  return rows.some(row => row.nonzeroBefore > 0) && rows.every(row => row.sampleRate === 48000 && row.missing === null
    && Number.isSafeInteger(row.outputCount) && row.outputCount > 0
    && row.failureFrameUpper !== null && row.zeroStart !== null && row.zeroEnd !== null
    && row.zeroStart <= row.failureFrameUpper + 128
    && row.zeroEnd - row.zeroStart >= row.sampleRate
    && row.outputClockFrame !== null && Number.isSafeInteger(row.outputClockFrame)
    && row.outputClockFrame >= row.zeroStart + row.sampleRate
    && row.nonzeroAfter === 0)
}
