import assert from 'node:assert/strict'
import {expect, test} from 'vitest'
import {packetRendererSource} from './livekit/packet-renderer'
import {PostGainOutputAudit, postGainAuditSource, type GainAuditInterval} from './livekit/post-gain-audit'

const row = (start = 48000, n = 0, first: number | null = null, last: number | null = null): GainAuditInterval => ({startFrame: start, endFrame: start + 128,
  nonzeroSamples: n, firstNonzeroFrame: first, lastNonzeroFrame: last})
const full = (start = 48000) => row(start, 128, start, start + 127)
const push = (a: PostGainOutputAudit, rows: GainAuditInterval[]) => a.record({kind: 'output', intervals: rows, confirmedFrame: rows.at(-1)!.startFrame})
const finish = (a: PostGainOutputAudit) => a.record({kind: 'finished', endFrame: 48256})
const poll = (a: PostGainOutputAudit, observed = 2100) => a.poll({contextTime: 2, performanceTime: 2000}, 48000, observed)
test('停止前の音声と停止後の観測済み無音を分ける', () => {
  const a = new PostGainOutputAudit(); push(a, [full(), row(48128)]);
  a.markCancelled({lowerMs: 1003.1, upperMs: 1003.1}, 1003.1); finish(a); poll(a)
  assert.equal(a.snapshot().complete, true); assert.equal(a.snapshot().nonzeroSamplesAfterCancelUpper, 0)
  a.markCancelled({lowerMs: 1003.1, upperMs: 1003.1}, 2200); assert.equal(a.snapshot().missingReason, null)
})
test('停止前にrenderされた未出力の旧音声を後から数える', () => {
  const a = new PostGainOutputAudit(); push(a, [row(), full(48128)])
  a.markCancelled({lowerMs: 1001.3, upperMs: 1001.3}, 1001.3); finish(a); poll(a)
  assert.equal(a.snapshot().complete, true)
  assert.equal(a.snapshot().nonzeroSamplesAfterCancelLower, 128)
  assert.equal(a.snapshot().nonzeroSamplesAfterCancelUpper, 128)
})
test('cancel境界をまたぐ1 sampleをゼロと断定しない', () => {
  const a = new PostGainOutputAudit(); push(a, [row(48000, 1, 48010, 48010), row(48128)])
  a.markCancelled({lowerMs: 1000.218, upperMs: 1000.218}, 1000.218); finish(a); poll(a)
  assert.equal(a.snapshot().nonzeroSamplesAfterCancelLower, 0)
  assert.equal(a.snapshot().nonzeroSamplesAfterCancelUpper, 1)
  assert.equal(a.snapshot().boundaryUncertainIntervals, 1)
})
test('音声出力時計とperformance時計の双方が末尾を通るまで待つ', () => {
  const a = new PostGainOutputAudit(); push(a, [row(), row(48128)])
  a.markCancelled({lowerMs: 1001, upperMs: 1001}, 1001); finish(a); poll(a, 1001)
  assert.equal(a.snapshot().complete, false); assert.equal(a.snapshot().observedIntervals, 0)
  poll(a); assert.equal(a.snapshot().complete, true)
})
test('観測がcancel時点を含まない場合は完了にしない', () => {
  for (const time of [999, 1010]) {
    const a = new PostGainOutputAudit(); push(a, [row(), row(48128)])
    a.markCancelled({lowerMs: time, upperMs: time}, time); finish(a); poll(a)
    assert.equal(a.snapshot().complete, false)
  }
})
test('出力区間の欠落・重複を明示欠測にする', () => {
  for (const next of [48000, 48256]) {
    const a = new PostGainOutputAudit(); push(a, [row(), row(next)])
    assert.equal(a.snapshot().missingReason, 'audit_output_gap')
  }
})
test('stop相当の早期closeで未出力を消してゼロにしない', () => {
  const a = new PostGainOutputAudit(); push(a, [row(), full(48128)])
  a.markCancelled({lowerMs: 1001, upperMs: 1001}, 1001); finish(a); a.close()
  assert.equal(a.snapshot().complete, false); assert.equal(a.snapshot().missingReason, 'audit_closed_before_drain')
})
test('過去のcancel後付けと時計逆行を欠測にする', () => {
  const a = new PostGainOutputAudit(); push(a, [row()]); poll(a)
  a.markCancelled({lowerMs: 1001, upperMs: 1001}, 2200)
  assert.equal(a.snapshot().missingReason, 'audit_cancel_clock_invalid')
  const b = new PostGainOutputAudit(); poll(b); b.poll({contextTime: 1, performanceTime: 1000}, 48000, 2200)
  assert.equal(b.snapshot().missingReason, 'audit_output_clock_invalid')
})
test('整合しない非ゼロsample情報やfinishを拒否する', () => {
  const a = new PostGainOutputAudit(); push(a, [row(48000, 1, 48010, 48020)])
  assert.equal(a.snapshot().missingReason, 'audit_interval_invalid')
  const b = new PostGainOutputAudit(); push(b, [row()]); finish(b)
  assert.equal(b.snapshot().missingReason, 'audit_finish_invalid')
})
test('滞留上限で欠測を残す', () => {
  const a = new PostGainOutputAudit()
  for (let i = 0; i < 1001; i++) push(a, [row(48000 + i * 128)])
  assert.equal(a.snapshot().missingReason, 'audit_observation_overflow')
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
