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

test('700msを超えて継続する弱い背景音を補助だけで確定しない', () => {
  const h = setup(); for (let i = 0; i < 8; i++) h.frame(0.01)
  h.frame(0, 700)
  expect(h.events.map(e => e.type)).toEqual(['candidate', 'misfire'])
})

test.each([
  {...supported, voicedFraction: 0.59}, {...supported, tonalConcentration: 0.9},
  {...supported, spectralFlatness: 0.3}, {...supported, voicedFraction: 1.01},
  {...supported, tonalConcentration: -1}, {...supported, spectralFlatness: Number.NaN},
])('雑音や不正な補助値を確定根拠へ加えない: %j', evidence => {
  const h = setup(); h.frame(0.01, 288, evidence); h.frame(0, 700)
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
