import { describe, expect, test } from 'vitest'
import { UtteranceDetector, type UtteranceDetection } from './utterance-detector'

const harness = () => {
  const events: UtteranceDetection[] = []
  const detector = new UtteranceDetector(event => events.push(event))
  let atMs = 0
  const frame = (amplitude: number, probability: number, durationMs = 96) => {
    atMs += durationMs
    detector.process(new Float32Array(durationMs * 16).fill(amplitude), probability, atMs)
  }
  return { detector, events, frame }
}

describe('PCMで終了境界を測る発話検出', () => {
  test('Sileroの確率の余韻で無音終了を遅らせず、元の語頭時刻を保持する', () => {
    const { events, frame } = harness()
    frame(0, 0); frame(0, 0)
    for (let i = 0; i < 4; i++) frame(0.01, 0.8)
    expect(events.find(event => event.type === 'confirmed')).toMatchObject({ speechStartedAtMs: 192 })
    for (let i = 0; i < 6; i++) frame(0, 0.9)
    expect(events.some(event => event.type === 'ended')).toBe(false)
    frame(0, 0.9)
    expect(events.at(-1)).toEqual({ type: 'ended', speechStartedAtMs: 192, detectedAtMs: 1248 })
  })

  test('600msの無音の直後に文が続けば1つの発話として保持する', () => {
    const { events, frame } = harness()
    for (let i = 0; i < 4; i++) frame(0.01, 0.8)
    frame(0, 0, 600)
    expect(events.some(event => event.type === 'ended')).toBe(false)
    for (let i = 0; i < 4; i++) frame(0.01, 0.8)
    for (let i = 0; i < 7; i++) frame(0, 0)
    expect(events.filter(event => event.type === 'confirmed')).toHaveLength(1)
    expect(events.filter(event => event.type === 'ended')).toHaveLength(1)
  })

  test('PCMが無音ならモデル確率だけでは発話を作らない', () => {
    const { events, frame } = harness()
    for (let i = 0; i < 30; i++) frame(0, 1)
    expect(events).toEqual([])
  })

  test('弱い確率だけの音声候補を勝手に確定しない', () => {
    const { events, frame } = harness()
    for (let i = 0; i < 10; i++) frame(0.01, 0.1)
    for (let i = 0; i < 7; i++) frame(0, 0.1)
    expect(events.map(event => event.type)).toEqual(['candidate', 'misfire'])
  })

  test('単発の衝撃音は確率が残っていても発話にしない', () => {
    const { events, frame } = harness()
    frame(0.5, 0.9)
    for (let i = 0; i < 7; i++) frame(0, 0.9)
    expect(events.map(event => event.type)).toEqual(['candidate', 'misfire'])
  })

  test('入力停止時のresetで前の候補を次の発話へ引き継がない', () => {
    const { detector, events, frame } = harness()
    frame(0.01, 0.8)
    detector.reset()
    for (let i = 0; i < 4; i++) frame(0.01, 0.8)
    expect(events.find(event => event.type === 'confirmed')).toMatchObject({ speechStartedAtMs: 96 })
  })

  test('環境音がPCM閾値を超え続けても発話後に低確率が続けば終了する', () => {
    const { events, frame } = harness()
    for (let i = 0; i < 4; i++) frame(0.1, 0.8)
    for (let i = 0; i < 8; i++) frame(0.01, 0.05)
    expect(events.map(event => event.type)).toEqual(['candidate', 'confirmed', 'ended'])
    expect(events.at(-1)?.detectedAtMs).toBe(1152)
  })

  test('中間確率の背景音に散在するピークを無期限に足して確定しない', () => {
    const { events, frame } = harness()
    for (let i = 0; i < 200; i++) frame(0.01, i % 12 === 0 ? 0.5 : 0.27)
    expect(events.some(event => event.type === 'confirmed')).toBe(false)
  })

  test('発話継続で低確率の終了待ちを解除し、600msの休止を分割しない', () => {
    const { events, frame } = harness()
    for (let i = 0; i < 4; i++) frame(0.1, 0.8)
    for (let i = 0; i < 6; i++) frame(0.01, 0.05)
    for (let i = 0; i < 4; i++) frame(0.1, 0.8)
    expect(events.filter(event => event.type === 'ended')).toHaveLength(0)
    for (let i = 0; i < 8; i++) frame(0.01, 0.05)
    expect(events.filter(event => event.type === 'ended')).toHaveLength(1)
  })

})
