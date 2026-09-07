import {expect, test} from 'vitest'
import {clockOffset, faultToBrowserOffset, faultTimeInBrowser, recoveryLatencyUpperMs,
  type ClockSample} from '../playwright/fault-clock'

const samples = (remote: number, rtt = 1): ClockSample[] => Array.from({length: 5}, (_, i) =>
  ({sentAtMs: 100 + i * 2, remoteAtMs: remote + i * 2, receivedAtMs: 100 + i * 2 + rtt}))
const before = {browser: samples(20), faultRunner: samples(10000)}

test('別epochの時計を上下限付きで対応させ、ns精度をJSON丸めで失わない', () => {
  const offset = faultToBrowserOffset(before, before)
  expect(offset.lowerMs).toBeCloseTo(-9981.4)
  expect(offset.upperMs).toBeCloseTo(-9978.6)
  const mapped = faultTimeInBrowser('10100000001', offset)
  expect(mapped.lowerMs).toBeCloseTo(118.600001)
  expect(mapped.upperMs).toBeCloseTo(121.400001)
})

test('時計driftとpage遷移によるepoch変更を合格へ補完しない', () => {
  expect(() => faultToBrowserOffset(before, {...before, browser: samples(5)})).toThrow('inconsistent')
  expect(() => faultToBrowserOffset(before, {...before, faultRunner: samples(10010)})).toThrow('inconsistent')
})

test('20msを超える合成誤差を拒否する', () => {
  const wide = {browser: samples(20, 10), faultRunner: samples(10000, 10)}
  expect(() => faultToBrowserOffset(wide, wide)).toThrow('20ms')
})

test.each([NaN, Infinity, -1])('不正な時計値を拒否する: %s', value => {
  expect(() => clockOffset([...samples(20), {sentAtMs: value, remoteAtMs: 0, receivedAtMs: 101}])).toThrow()
})

test('欠損sampleと逆転した因果関係を拒否する', () => {
  expect(() => clockOffset(samples(20).slice(1))).toThrow('missing')
  expect(() => clockOffset([...samples(20), {sentAtMs: 101, remoteAtMs: 0, receivedAtMs: 100}])).toThrow('invalid')
})

test.each(['1.2', '-1', 'NaN', '9007199254740992000000'])('不正なns値を拒否する: %s', ns => {
  expect(() => faultTimeInBrowser(ns, {lowerMs: 0, upperMs: 1})).toThrow()
})

test('復旧後のackでも復旧前または不確実区間内の要求なら除外する', () => {
  for (const sent of [99, 100, 101, 102]) {
    expect(() => recoveryLatencyUpperMs({lowerMs: 100, upperMs: 102}, sent, 110)).toThrow()
  }
  expect(recoveryLatencyUpperMs({lowerMs: 100, upperMs: 102}, 103, 110)).toBe(10)
  expect(() => recoveryLatencyUpperMs({lowerMs: 100, upperMs: 102}, 110, 109)).toThrow()
})
