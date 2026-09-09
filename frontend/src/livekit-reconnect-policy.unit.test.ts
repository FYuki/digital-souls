import {expect, test} from 'vitest'
import {VoiceReconnectPolicy, type RetryObservation} from './livekit/reconnect-policy'

test('短時間障害は500ms以内に再試行し、後半はbackoffしつつ60秒を超えない', () => {
  for (const random of [0, .5, 1]) {
    let now = 100
    const policy = new VoiceReconnectPolicy(() => random, undefined, () => now)
    expect(policy.nextRetryDelayInMs({retryCount: 0, elapsedMs: 0})).toBe(0)
    for (const elapsedMs of [300, 1999, 9999]) {
      now = 100 + elapsedMs
      const delay = policy.nextRetryDelayInMs({retryCount: 4, elapsedMs})!
      expect(delay).toBeGreaterThanOrEqual(250)
      expect(delay).toBeLessThanOrEqual(500)
    }
    now = 10100
    expect(policy.nextRetryDelayInMs({retryCount: 10, elapsedMs: 10000})).toBeGreaterThanOrEqual(1000)
    now = 60099
    expect(policy.nextRetryDelayInMs({retryCount: 15, elapsedMs: 59999})).toBe(1)
    now = 60100
    expect(policy.nextRetryDelayInMs({retryCount: 16, elapsedMs: 0})).toBeNull()
  }
})

test('失敗が続いても回数で停止し、URLや例外の内容を観測へ流さない', () => {
  const rows: RetryObservation[] = []
  let now = 0
  const policy = new VoiceReconnectPolicy(() => .5, row => rows.push(row), () => now)
  expect(policy.nextRetryDelayInMs({retryCount: 40, elapsedMs: 20000})).toBeNull()
  policy.nextRetryDelayInMs({retryCount: 0, elapsedMs: 0})
  now = 200
  policy.nextRetryDelayInMs({retryCount: 1, elapsedMs: 200, serverUrl: 'private-sentinel', retryReason: new Error('private-sentinel')})
  expect(rows).toHaveLength(3)
  expect(JSON.stringify(rows)).not.toContain('private-sentinel')
  expect(rows[2]).toEqual({retryCount: 1, elapsedMs: 200, delayMs: 375})
})

test('壁時計の巻き戻りでも再試行を打ち切らず、進みでも猶予を短縮しない', () => {
  let now = 1000
  const rows: RetryObservation[] = []
  const policy = new VoiceReconnectPolicy(() => .5, row => rows.push(row), () => now)
  policy.nextRetryDelayInMs({retryCount: 0, elapsedMs: 0})
  now = 1806
  expect(policy.nextRetryDelayInMs({retryCount: 3, elapsedMs: -977})).toBe(375)
  expect(rows.at(-1)?.elapsedMs).toBe(806)
  now = 2000
  expect(policy.nextRetryDelayInMs({retryCount: 4, elapsedMs: 120000})).toBe(375)
  now = 61000
  expect(policy.nextRetryDelayInMs({retryCount: 5, elapsedMs: -1000})).toBeNull()
  now = 70000
  expect(policy.nextRetryDelayInMs({retryCount: 0, elapsedMs: 0})).toBe(0)
  now = 70100
  expect(policy.nextRetryDelayInMs({retryCount: 1, elapsedMs: 100})).toBe(375)
})

test.each([{retryCount: -1, elapsedMs: 0}, {retryCount: .1, elapsedMs: 0}, {retryCount: 0, elapsedMs: NaN}])(
  '不正なSDK時計・回数は再試行しない', context => {
    expect(new VoiceReconnectPolicy().nextRetryDelayInMs(context)).toBeNull()
  })

test.each([NaN, -1, Infinity])('不正な単調時計では再試行しない', now => {
  expect(new VoiceReconnectPolicy(() => .5, undefined, () => now)
    .nextRetryDelayInMs({retryCount: 0, elapsedMs: 0})).toBeNull()
})

test('単調時計が逆転した場合は猶予を延長せず停止する', () => {
  let now = 100
  const policy = new VoiceReconnectPolicy(() => .5, undefined, () => now)
  policy.nextRetryDelayInMs({retryCount: 0, elapsedMs: 0})
  now = 99
  expect(policy.nextRetryDelayInMs({retryCount: 1, elapsedMs: 1})).toBeNull()
})
