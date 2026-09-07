import {expect, test} from 'vitest'
import {UtteranceDetector, type UtteranceDetection} from './utterance-detector'

const supported = {voicedFraction: 1, tonalConcentration: 0.5, spectralFlatness: 0.05}
const setup = () => {
  const events: UtteranceDetection[] = []
  const detector = new UtteranceDetector(event => events.push(event))
  let now = 0
  const frame = (amplitude: number, ms = 96, evidence = supported, probability = 0.05) => {
    now += ms
    detector.process(new Float32Array(ms * 16).fill(amplitude), probability, now, evidence)
  }
  return {events, detector, frame}
}

test('短い音声は静音終了まで保留し、元の開始時刻で開始・終了を順に通知する', () => {
  const h = setup()
  h.frame(0); h.frame(0.01); h.frame(0.01)
  expect(h.events.map(e => e.type)).toEqual(['candidate'])
  h.frame(0, 600)
  expect(h.events.map(e => e.type)).toEqual(['candidate'])
  h.frame(0, 10)
  expect(h.events.slice(-2)).toEqual([
    {type: 'confirmed', speechStartedAtMs: 96, detectedAtMs: 898},
    {type: 'ended', speechStartedAtMs: 96, detectedAtMs: 898},
  ])
})

test('600msの休止後に続く発話を補助判定で分割しない', () => {
  const h = setup()
  h.frame(0.01); h.frame(0.01); h.frame(0, 600); h.frame(0.01, 96, supported, 0.9)
  expect(h.events.filter(e => e.type === 'ended')).toHaveLength(0)
})

test.each([96, 159])('補助根拠%s msでは短い衝撃音を確定しない', ms => {
  const h = setup(); h.frame(0.01, ms); h.frame(0, 700)
  expect(h.events.map(e => e.type)).toEqual(['candidate', 'misfire'])
})

test('1秒を超えて継続する弱い背景音を補助だけで確定しない', () => {
  const h = setup(); h.frame(0.01, 1001)
  h.frame(0, 700)
  expect(h.events.some(e => e.type === 'confirmed')).toBe(false)
  h.frame(0, 400)
  expect(h.events.map(e => e.type)).toEqual(['candidate', 'misfire'])
})

test.each([
  {...supported, voicedFraction: 0.59}, {...supported, tonalConcentration: 0.9},
  {...supported, spectralFlatness: 0.3}, {...supported, voicedFraction: 1.01},
  {...supported, tonalConcentration: -1}, {...supported, spectralFlatness: Number.NaN},
])('雑音や不正な補助値を確定根拠へ加えない: %j', evidence => {
  const h = setup(); h.frame(0.01, 288, evidence); h.frame(0, 700)
  expect(h.events.some(e => e.type === 'confirmed')).toBe(false)
  // 有声の母音に近い集中度でも確定せず、保留の上限では破棄する。
  h.frame(0, 1100)
  expect(h.events.map(e => e.type)).toEqual(['candidate', 'misfire'])
})

test('reset前の補助根拠を次の候補へ引き継がない', () => {
  const h = setup(); h.frame(0.01); h.detector.reset(); h.frame(0.01); h.frame(0, 700)
  expect(h.events.some(e => e.type === 'confirmed')).toBe(false)
})

test('通常のSilero確定を維持し、補助判定で通知を重複させない', () => {
  const h = setup(); for (let i = 0; i < 4; i++) h.frame(0.01, 96, supported, 0.9)
  h.frame(0, 700)
  expect(h.events.map(e => e.type)).toEqual(['candidate', 'confirmed', 'ended'])
})


test.each([768, 960, 1000])('1秒以内の弱い発話%s msも補助根拠があれば終了時に確定する', ms => {
  const h = setup()
  h.frame(0.01, ms)
  h.frame(0, 600)
  expect(h.events.map(event => event.type)).toEqual(['candidate'])
  h.frame(0, 96)
  expect(h.events.slice(-2)).toEqual([
    {type: 'confirmed', speechStartedAtMs: 0, detectedAtMs: ms + 696},
    {type: 'ended', speechStartedAtMs: 0, detectedAtMs: ms + 696},
  ])
  // Coreの2秒pre-rollに発話先頭と終了までの無音を収める。
  expect(h.events.at(-1)!.detectedAtMs - h.events.at(-1)!.speechStartedAtMs).toBeLessThan(2000)
})


const concentratedVowel = {...supported, tonalConcentration: 0.97}

test('未確定の短い母音は後続語を待ち、元の開始時刻をpre-roll内で保持する', () => {
  const h = setup()
  h.frame(0.015, 288, concentratedVowel, 0.1)
  h.frame(0, 700)
  expect(h.events.map(e => e.type)).toEqual(['candidate'])
  h.frame(0.04, 384, supported, 0.8)
  expect(h.events.at(-1)).toEqual({type: 'confirmed', speechStartedAtMs: 0, detectedAtMs: 1372})
  h.frame(0, 700)
  expect(h.events.map(e => e.type)).toEqual(['candidate', 'confirmed', 'ended'])
})

test('保留した母音だけでは確定せず、2秒を超えて次の発話へ引き継がない', () => {
  const h = setup()
  h.frame(0.015, 288, concentratedVowel, 0.1)
  h.frame(0, 1712)
  expect(h.events).toEqual([
    {type: 'candidate', speechStartedAtMs: 0, detectedAtMs: 288},
    {type: 'misfire', speechStartedAtMs: 0, detectedAtMs: 2000},
  ])
  h.frame(0.04, 384, supported, 0.8)
  expect(h.events.at(-1)).toMatchObject({type: 'confirmed', speechStartedAtMs: 2000})
})

test('モデルが低確率でも補助VADが有声とする確定済み発話を分割しない', () => {
  const h = setup()
  h.frame(0.05, 384, supported, 0.8)
  h.frame(0.03, 1200, supported, 0.1)
  expect(h.events.map(e => e.type)).toEqual(['candidate', 'confirmed'])
  // 単音や背景音しか残らなければ従来の700msによる終了を維持する。
  h.frame(0.01, 768, concentratedVowel, 0.05)
  expect(h.events.map(e => e.type)).toEqual(['candidate', 'confirmed', 'ended'])
})

test('確定後のPCM静音は補助VADの余韻があっても600msで終了を判定する', () => {
  const h = setup()
  h.frame(0.05, 384, supported, 0.8)
  h.frame(0, 600, supported, 0.8)
  expect(h.events.map(e => e.type)).toEqual(['candidate', 'confirmed'])
  h.frame(0, 96, supported, 0.8)
  expect(h.events.at(-1)).toMatchObject({type: 'ended', detectedAtMs: 1080})
})
