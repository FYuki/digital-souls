import { createHash } from 'node:crypto'
import { runInNewContext } from 'node:vm'
import { describe, expect, test } from 'vitest'
import type { Page } from '@playwright/test'
import { fixtureWorkletSource, parseScheduledFixture, readFixtureBounds, type ScheduledFixture } from './controlled-audio-fixture'

type Port = {
  onmessage: ((event: { data: { type: string; sentAtMs: number; fixture?: Partial<ScheduledFixture> } }) => void) | null
  postMessage: (data: unknown) => void
}
type Processor = { port: Port; process: (inputs: unknown[], outputs: Float32Array[][]) => boolean }
const processor = (samples: number[], start: number, end: number) => {
  const events: { kind: string; lowerMs: number; sourceSample: number }[] = []
  const registry: { factory?: new (options: { processorOptions: Partial<ScheduledFixture> }) => Processor } = {}
  class AudioWorkletProcessor {
    port: Port = { onmessage: null, postMessage: data => { if ((data as { kind: string }).kind !== 'finished') events.push(data as typeof events[number]) } }
  }
  runInNewContext(fixtureWorkletSource, {
    AudioWorkletProcessor,
    registerProcessor: (_name: string, constructor: NonNullable<typeof registry.factory>) => { registry.factory = constructor },
  })
  const Constructor = registry.factory
  if (!Constructor) throw new Error('worklet registration missing')
  const instance = new Constructor({ processorOptions: { samples, speechStartSample: start, speechEndSample: end } })
  const next = (count: number) => {
    const out = new Float32Array(count)
    instance.process([], [[out]])
    return [...out]
  }
  const send = (type: string, sentAtMs: number, fixture?: Partial<ScheduledFixture>) => instance.port.onmessage!({ data: { type, sentAtMs, fixture } })
  return { events, next, send }
}

// 固定信号の完全一致で、WebRTCより前にfixtureを変形していないことを確認する。
describe('観測可能なfixture音声源', () => {
  test('開始要求まで無音を出し、fixtureを先に消費しない', () => {
    const p = processor([0, 0.25, -0.5, 0], 1, 3)
    expect(p.next(4)).toEqual([0, 0, 0, 0])
    expect(p.events).toEqual([])
    p.send('start', 100)
    expect(p.next(4)).toEqual([0, 0.25, -0.5, 0])
    expect(p.events.map(e => e.kind)).toEqual(['sourceStart', 'speechStart', 'speechEnd'])
    expect(p.events.every(e => e.lowerMs === 100)).toBe(true)
  })

  test('quantum境界の発話末尾と次quantumの先頭を取り違えない', () => {
    const p = processor([0, 0, 0, 0.25, 0.5, -0.25, 0, 0], 3, 4)
    p.send('start', 10)
    expect(p.next(4)).toEqual([0, 0, 0, 0.25])
    expect(p.events.find(e => e.kind === 'speechEnd')).toEqual({ kind: 'speechEnd', lowerMs: 10, sourceSample: 4 })
    p.send('ping', 14)
    expect(p.next(4)).toEqual([0.5, -0.25, 0, 0])
    expect(p.next(4)).toEqual([0, 0, 0, 0])
    expect(p.events).toHaveLength(3)
  })

  test('後続の境界には、そのprocessより前に届いたpingの時刻を使う', () => {
    const p = processor([0, 0, 0.25, 0.5, -0.25, 0.25, 0, 0], 2, 6)
    p.send('start', 100)
    p.next(4)
    p.send('ping', 105)
    p.next(4)
    expect(p.events.find(e => e.kind === 'speechStart')?.lowerMs).toBe(100)
    expect(p.events.find(e => e.kind === 'speechEnd')?.lowerMs).toBe(105)
  })

  test('開始を重複要求しても音声を巻き戻さない', () => {
    const p = processor([0.25, 0.5, -0.5, -0.25], 0, 4)
    p.send('start', 100)
    expect(p.next(2)).toEqual([0.25, 0.5])
    p.send('start', 105)
    expect(p.next(2)).toEqual([-0.5, -0.25])
    expect(p.next(2)).toEqual([0, 0])
    expect(p.events.filter(e => e.kind === 'sourceStart')).toHaveLength(1)
  })
  test('全sample消費後の明示replayだけが同じ音声を新しい境界で再送する', () => {
    const p = processor([0.25, 0.5, -0.5, -0.25], 0, 4)
    p.send('start', 100)
    expect(p.next(2)).toEqual([0.25, 0.5])
    p.send('replay', 105)
    expect(p.next(2)).toEqual([-0.5, -0.25])
    expect(p.next(2)).toEqual([0, 0])
    p.send('replay', 200)
    expect(p.next(4)).toEqual([0.25, 0.5, -0.5, -0.25])
    expect(p.events.filter(e => e.kind === 'sourceStart').map(e => e.lowerMs)).toEqual([100, 200])
  })

  test('前の音声を消費してから別fixtureへ切り替え、境界とPCMを混ぜない', () => {
    const p = processor([0.25, 0.5, -0.5, -0.25], 0, 4)
    const next = {samples: [0, -0.75, 0.75, 0, 0, 0], speechStartSample: 1, speechEndSample: 3}
    p.send('start', 100)
    expect(p.next(2)).toEqual([0.25, 0.5])
    p.send('replay', 105, next)
    expect(p.next(2)).toEqual([-0.5, -0.25])
    p.send('replay', 200, next)
    expect(p.next(4)).toEqual([0, -0.75, 0.75, 0])
    expect(p.next(4)).toEqual([0, 0, 0, 0])
    expect(p.events.filter(e => e.kind === 'speechEnd')).toEqual([
      {kind: 'speechEnd', lowerMs: 100, sourceSample: 4},
      {kind: 'speechEnd', lowerMs: 200, sourceSample: 3},
    ])
  })

})

const wav = () => {
  const bytes = Buffer.alloc(52)
  bytes.write('RIFF'); bytes.writeUInt32LE(44, 4); bytes.write('WAVEfmt ', 8)
  bytes.writeUInt32LE(16, 16); bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(1, 22)
  bytes.writeUInt32LE(48000, 24); bytes.writeUInt32LE(96000, 28)
  bytes.writeUInt16LE(2, 32); bytes.writeUInt16LE(16, 34); bytes.write('data', 36); bytes.writeUInt32LE(8, 40)
  ;[0, 8192, -16384, 0].forEach((sample, i) => bytes.writeInt16LE(sample, 44 + 2 * i))
  return bytes
}
const metadata = (bytes: Uint8Array) => ({ audio_sha256: createHash('sha256').update(bytes).digest('hex'),
  sample_rate_hz: 48000, speech_start_sample: 1, speech_end_sample: 3 })

test('WAVのPCM16値を正規化するだけで余白も含めて保持する', () => {
  const bytes = wav()
  expect(parseScheduledFixture(bytes, metadata(bytes)).samples).toEqual([0, 0.25, -0.5, 0])
})

test('hashが異なる音声を同じ正解境界として使用しない', () => {
  const bytes = wav(), original = metadata(bytes)
  bytes.writeInt16LE(32767, 46)
  expect(() => parseScheduledFixture(bytes, original)).toThrow('hash mismatch')
})

test('ステレオ音声や範囲外の境界をmonoとして解釈しない', () => {
  const bytes = wav()
  expect(() => parseScheduledFixture(bytes, { ...metadata(bytes), speech_end_sample: 100 })).toThrow('boundaries')
  bytes.writeUInt16LE(2, 22)
  expect(() => parseScheduledFixture(bytes, metadata(bytes))).toThrow('mono PCM16')
})


const clockPage = (speechEnd: object | undefined) => ({ evaluate: async () => ({
  sourceStart: { lowerMs: 10, upperMs: 11, sourceSample: 0 },
  speechStart: { lowerMs: 20, upperMs: 21, sourceSample: 480 }, speechEnd,
}) }) as unknown as Page

test.each([
  undefined,
  { lowerMs: 100, upperMs: 121, sourceSample: 960 },
  { lowerMs: 100, upperMs: 99, sourceSample: 960 },
  { lowerMs: -1, upperMs: 1, sourceSample: 960 },
])('欠測・幅超過・逆転した正解境界を採用しない: %s', async speechEnd => {
  await expect(readFixtureBounds(clockPage(speechEnd))).rejects.toThrow('unavailable or uncertain')
})

test('許容範囲内でも片側の時刻へ丸めず上下限を維持する', async () => {
  const bounds = await readFixtureBounds(clockPage({ lowerMs: 100, upperMs: 103, sourceSample: 960 }))
  expect(bounds.speechEnd).toEqual({ lowerMs: 100, upperMs: 103, sourceSample: 960 })
})
