import assert from 'node:assert/strict'
import {expect, test} from 'vitest'
import {packetRendererSource} from './livekit/packet-renderer'
import {PostGainOutputAudit, postGainAuditSource, type GainAuditInterval} from './livekit/post-gain-audit'

const row = (start = 48000, n = 0, first: number | null = null, last: number | null = null): GainAuditInterval => ({
  startFrame: start, endFrame: start + 128, nonzeroSamples: n, firstNonzeroFrame: first, lastNonzeroFrame: last})
const full = (start: number) => row(start, 128, start, start + 127)
const push = (a: PostGainOutputAudit, rows: GainAuditInterval[]) => a.record({kind: 'output', intervals: rows, confirmedFrame: rows.at(-1)!.startFrame})
const finish = (a: PostGainOutputAudit) => a.record({kind: 'finished', endFrame: 50048})
const before = (a: PostGainOutputAudit) => a.poll({contextTime: 1.009, performanceTime: 1009}, 48000, 1009.5)
const after = (a: PostGainOutputAudit) => a.poll({contextTime: 1.012, performanceTime: 1012}, 48000, 1012.5)
const drain = (a: PostGainOutputAudit) => {finish(a); a.poll({contextTime: 1.05, performanceTime: 1050}, 48000, 1051)}
function fixture(replacements: GainAuditInterval[] = []) {
  const a = new PostGainOutputAudit()
  const rows = Array.from({length: 16}, (_, i) => replacements.find(r => r.startFrame === 48000 + i * 128) ?? row(48000 + i * 128))
  push(a, rows); before(a); a.markCancelled({lowerMs: 1010, upperMs: 1010}, 1010)
  return a
}

// 1.009秒のbinary浮動小数点と外側1 sampleの余裕を含め、lowerは48430へ丸める。
test('cancel前後の観測点からframe上下限を求め、前の音声をstaleへ数えない', () => {
  const a = fixture([full(48000)]); after(a); drain(a)
  expect(a.snapshot()).toMatchObject({complete: true, missingReason: null, nonzeroSamplesAfterCancelUpper: 0,
    cancelOutputFrameBounds: {lowerFrame: 48430, upperFrame: 48577}, clockCorrelationMethod: 'bracketing_output_timestamps'})
  a.markCancelled({lowerMs: 1010, upperMs: 1010}, 1100)
  expect(a.snapshot().complete).toBe(true)
})
test('cancel時にrender済みで出力待ちの旧音声を落とさない', () => {
  const a = fixture([full(48768)]); after(a); drain(a)
  expect(a.snapshot()).toMatchObject({complete: true, nonzeroSamplesAfterCancelLower: 128, nonzeroSamplesAfterCancelUpper: 128})
})
test('境界の範囲内にある1 sampleをゼロと断定しない', () => {
  const a = fixture([row(48512, 1, 48520, 48520)]); after(a); drain(a)
  expect(a.snapshot()).toMatchObject({complete: true, nonzeroSamplesAfterCancelLower: 0,
    nonzeroSamplesAfterCancelUpper: 1, boundaryUncertainIntervals: 1})
})
test('cancel後の観測点がまだない間に通過した区間も分類まで保持する', () => {
  const a = fixture([full(48384)])
  a.poll({contextTime: 1.012, performanceTime: 1009.9}, 48000, 1010.5)
  expect(a.snapshot().cancelOutputFrameBounds).toBeNull()
  a.poll({contextTime: 1.013, performanceTime: 1013}, 48000, 1013.5); drain(a)
  expect(a.snapshot()).toMatchObject({complete: true, nonzeroSamplesAfterCancelUpper: 82, nonzeroSamplesAfterCancelLower: 0})
})
test('未来のtimestampを観測済みのanchorや出力済みへ採用しない', () => {
  const a = fixture([full(48768)])
  a.poll({contextTime: 1.012, performanceTime: 1012}, 48000, 1011)
  expect(a.snapshot().cancelOutputFrameBounds).toBeNull()
  expect(a.snapshot().observedIntervals).toBe(3)
  after(a); drain(a); expect(a.snapshot().complete).toBe(true)
})
test('cancel前anchorの欠測や観測窓不足はdrain成功と区別する', () => {
  const a = new PostGainOutputAudit(); push(a, [row()]); a.markCancelled({lowerMs: 1010, upperMs: 1010}, 1010)
  expect(a.snapshot()).toMatchObject({complete: false, missingReason: 'audit_cancel_anchor_missing'})
  const b = fixture(); b.poll({contextTime: 1.06, performanceTime: 1060}, 48000, 1061); finish(b)
  b.poll({contextTime: 1.07, performanceTime: 1070}, 48000, 1071)
  expect(b.snapshot()).toMatchObject({complete: false, missingReason: 'audit_cancel_window_unobserved'})
})
test('sample区間の欠落・重複を明示欠測にする', () => {
  for (const next of [48000, 48256]) {
    const a = new PostGainOutputAudit(); push(a, [row(), row(next)])
    expect(a.snapshot().missingReason).toBe('audit_output_gap')
  }
})
test('早期closeで未出力を消してゼロにしない', () => {
  const a = fixture([full(48768)]); after(a); finish(a); a.close()
  expect(a.snapshot()).toMatchObject({complete: false, missingReason: 'audit_closed_before_drain'})
})
test('過去のcancel後付けと実timestamp逆行を拒否する', () => {
  const a = new PostGainOutputAudit(); before(a); a.markCancelled({lowerMs: 1008, upperMs: 1008}, 1100)
  expect(a.snapshot().missingReason).toBe('audit_cancel_clock_invalid')
  const b = new PostGainOutputAudit(); before(b)
  b.poll({contextTime: 1.008, performanceTime: 1010}, 48000, 1011)
  expect(b.snapshot()).toMatchObject({missingReason: 'audit_output_clock_invalid', clockFailure: {stage: 'timestamp_regression'}})
})
test('整合しないsample情報とfinishを拒否する', () => {
  const a = new PostGainOutputAudit(); push(a, [row(48000, 1, 48010, 48020)])
  expect(a.snapshot().missingReason).toBe('audit_interval_invalid')
  const b = new PostGainOutputAudit(); push(b, [row()]); finish(b)
  expect(b.snapshot().missingReason).toBe('audit_finish_invalid')
})
test('未出力区間が滞留上限を越えた場合は欠測にする', () => {
  const a = new PostGainOutputAudit()
  for (let i = 0; i < 1001; i++) push(a, [row(48000 + i * 128)])
  expect(a.snapshot().missingReason).toBe('audit_observation_overflow')
})
test('別timestampでの外挿が0.232ms逆転しても、直接の前後anchorで範囲を保持する', () => {
  const a = fixture([row(48512, 1, 48520, 48520)])
  a.poll({contextTime: 1.012, performanceTime: 1011.768}, 48000, 1012.5); drain(a)
  expect(a.snapshot()).toMatchObject({complete: true, clockFailure: null, missingReason: null,
    cancelOutputFrameBounds: {lowerFrame: 48430, upperFrame: 48577},
    nonzeroSamplesAfterCancelLower: 0, nonzeroSamplesAfterCancelUpper: 1})
})

test('再生と監視workletを同じmoduleで登録し、波形を変えず観測する', () => {
  const p = auditProcessor()
  expect(p.registered).toEqual(['packet-renderer', 'post-gain-audit'])
  p.step(0)
  const signal = Float32Array.from({length: 128}, (_, i) => Math.sin(i) * .25)
  assert.deepEqual(p.step(128, signal), signal)
  for (let i = 2; i <= 16; i++) p.step(i * 128)
  expect(p.messages[0]).toMatchObject({kind: 'output', intervals: [
    {startFrame: 128, endFrame: 256, nonzeroSamples: 127, firstNonzeroFrame: 129, lastNonzeroFrame: 255},
    ...Array.from({length: 15}, (_, i) => ({startFrame: (i + 2) * 128, endFrame: (i + 3) * 128,
      nonzeroSamples: 0, firstNonzeroFrame: null, lastNonzeroFrame: null})),
  ]})
  expect(JSON.stringify(p.messages)).not.toContain('samples')
  p.send({kind: 'finish'}); p.step(2176)
  expect(p.messages.at(-1)).toEqual({kind: 'finished', endFrame: 2304})
  p.step(2304, signal)
  expect(p.messages.at(-1)).toMatchObject({kind: 'missing', reason: 'audit_output_after_finish'})
})

test('監視workletの時計が未照合ならfinish通知を保留する', () => {
  const p = auditProcessor(); p.step(0); p.step(128); p.step(128); p.send({kind: 'finish'})
  expect(p.messages).toEqual([]); p.step(384)
  expect(p.messages[0]).toMatchObject({kind: 'output', confirmedFrame: 384})
  expect(p.messages.at(-1)).toEqual({kind: 'finished', endFrame: 512})
})

test('監視workletの入力長不一致を例外や正常完了へ変換しない', () => {
  const p = auditProcessor(); p.step(0)
  expect(() => p.step(128, new Float32Array(256))).not.toThrow()
  expect(p.messages.at(-1)).toEqual({kind: 'missing', reason: 'audit_channel_mismatch'})
})

function auditProcessor() {
  const messages: Record<string, unknown>[] = [], registered: string[] = []
  class Base {
    port = {onmessage: null as ((event: {data: unknown}) => void) | null,
      postMessage: (row: Record<string, unknown>) => messages.push(row)}
  }
  type Processor = Base & {process: (inputs: Float32Array[][], outputs: Float32Array[][]) => boolean}
  let instance: Processor
  const setFrame = new Function('AudioWorkletProcessor', 'registerProcessor',
    'let currentFrame = 0;' + packetRendererSource + '\n' + postGainAuditSource + ';return value => {currentFrame = value}')(
      Base, (name: string, Kind: new (options: unknown) => Processor) => {
        registered.push(name); if (name === 'post-gain-audit') instance = new Kind({processorOptions: {}})
      },
    ) as (value: number) => void
  return {registered, messages, send: (data: unknown) => instance.port.onmessage?.({data}),
    step: (frame: number, pcm = new Float32Array(128)) => {
      setFrame(frame); const output = new Float32Array(128); instance.process([[pcm]], [[output]]); return output
    }}
}
