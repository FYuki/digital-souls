// @vitest-environment node
import { readFile } from 'node:fs/promises'
import { afterEach, describe, expect, test } from 'vitest'

import { createShortSpeechAnalyzer, spectralEvidence, type ShortSpeechAnalyzer } from './short-speech-evidence'

const analyzers: ShortSpeechAnalyzer[] = []
const binaryPath = new URL('./vendor/libfvad.wasm', import.meta.url)
const createAnalyzer = async () => {
  const bytes = await readFile(binaryPath)
  const analyzer = await createShortSpeechAnalyzer(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength))
  analyzers.push(analyzer)
  return analyzer
}
const tone = (...frequencies: number[]) => Float32Array.from({length: 1536}, (_, i) =>
  frequencies.reduce((sum, frequency) => sum + 0.1 * Math.sin(2 * Math.PI * frequency * i / 16000), 0))

afterEach(() => {
  for (const analyzer of analyzers.splice(0)) analyzer.close()
})

describe('短い発話の補助根拠', () => {
  test('同梱WASMで無音を処理し、閉じた後の再利用を拒否する', async () => {
    const analyzer = await createAnalyzer()
    for (let i = 0; i < 10; i++) {
      expect(analyzer.process(new Float32Array(1536))).toEqual({
        voicedFraction: 0, tonalConcentration: 1, spectralFlatness: 1,
      })
    }
    analyzer.close()
    analyzer.close()
    expect(() => analyzer.process(new Float32Array(1536))).toThrow('closed')
    expect(() => analyzer.reset()).toThrow('closed')
  })

  test('端数を持つ状態からのリセットは新しい解析器と同じ結果になる', async () => {
    const dirty = await createAnalyzer()
    const fresh = await createAnalyzer()
    dirty.process(tone(220, 440, 880))
    dirty.reset()
    const frames = [new Float32Array(1536), tone(180, 540, 900), tone(220, 440),
      ...Array.from({length: 8}, () => new Float32Array(1536))]
    expect(frames.map(frame => dirty.process(frame))).toEqual(frames.map(frame => fresh.process(frame)))
  })

  test('PCMを変更せず、有限値の範囲外振幅もクリップして処理する', async () => {
    const analyzer = await createAnalyzer()
    const frame = tone(180, 540, 900).map(value => value * 100)
    const original = frame.slice()
    const evidence = analyzer.process(frame)
    expect(frame).toEqual(original)
    for (const value of Object.values(evidence)) {
      expect(Number.isFinite(value)).toBe(true)
      expect(value).toBeGreaterThanOrEqual(0)
      expect(value).toBeLessThanOrEqual(1)
    }
  })

  test('不正な入力を拒否した後も状態を汚さず処理できる', async () => {
    const analyzer = await createAnalyzer()
    const fresh = await createAnalyzer()
    for (const frame of [new Float32Array(160), new Float32Array(1536).fill(NaN), new Float32Array(1536).fill(Infinity)]) {
      expect(() => analyzer.process(frame)).toThrow('Invalid')
    }
    const frame = tone(180, 540, 900)
    expect(analyzer.process(frame)).toEqual(fresh.process(frame))
  })

  test('単音・二音は集中度で、広帯域雑音は平坦度で識別できる', () => {
    for (const frame of [tone(440), tone(220, 440), tone(1000, 2000)]) {
      expect(spectralEvidence(frame).tonalConcentration).toBeGreaterThan(0.9)
    }
    let seed = 123456789
    const noise = Float32Array.from({length: 1536}, () => {
      seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0
      return seed / 2 ** 32 - 0.5
    })
    expect(spectralEvidence(noise).spectralFlatness).toBeGreaterThan(0.3)
    const harmonics = spectralEvidence(tone(180, 360, 540, 720, 900, 1080))
    expect(harmonics.tonalConcentration).toBeLessThan(0.9)
    expect(harmonics.spectralFlatness).toBeLessThan(0.3)
  })

  test('スペクトル計測は末尾1024標本だけを使用し、入力を変更しない', () => {
    const frame = tone(220, 440)
    const original = frame.slice()
    expect(spectralEvidence(frame)).toEqual(spectralEvidence(frame.slice(-1024)))
    expect(frame).toEqual(original)
    expect(() => spectralEvidence(new Float32Array(100))).toThrow('Invalid')
  })
})
