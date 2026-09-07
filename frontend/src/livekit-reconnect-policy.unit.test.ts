import {expect, test} from 'vitest'
import {VoiceReconnectPolicy, type RetryObservation} from './livekit/reconnect-policy'

test('短時間障害は500ms以内に再試行し、後半はbackoffしつつ60秒を超えない', () => {
  for (const random of [0, .5, 1]) {
    const policy = new VoiceReconnectPolicy(() => random)
    expect(policy.nextRetryDelayInMs({retryCount: 0, elapsedMs: 0})).toBe(0)
    for (const elapsedMs of [300, 1999, 9999]) {
      const delay = policy.nextRetryDelayInMs({retryCount: 4, elapsedMs})!
      expect(delay).toBeGreaterThanOrEqual(250)
      expect(delay).toBeLessThanOrEqual(500)
    }
    expect(policy.nextRetryDelayInMs({retryCount: 10, elapsedMs: 10000})).toBeGreaterThanOrEqual(1000)
    expect(policy.nextRetryDelayInMs({retryCount: 15, elapsedMs: 59999})).toBe(1)
  }
})

test('失敗が続いても回数と総時間で停止し、URLや例外の内容を観測へ流さない', () => {
  const rows: RetryObservation[] = []
  const policy = new VoiceReconnectPolicy(() => .5, row => rows.push(row))
  expect(policy.nextRetryDelayInMs({retryCount: 40, elapsedMs: 20000})).toBeNull()
  expect(policy.nextRetryDelayInMs({retryCount: 2, elapsedMs: 60000})).toBeNull()
  policy.nextRetryDelayInMs({retryCount: 1, elapsedMs: 200, serverUrl: 'private-sentinel', retryReason: new Error('private-sentinel')})
  expect(rows).toHaveLength(3)
  expect(JSON.stringify(rows)).not.toContain('private-sentinel')
  expect(rows[2]).toEqual({retryCount: 1, elapsedMs: 200, delayMs: 375})
})

test.each([{retryCount: -1, elapsedMs: 0}, {retryCount: .1, elapsedMs: 0}, {retryCount: 0, elapsedMs: NaN},
  {retryCount: 0, elapsedMs: -1}])('不正な時計・回数は再試行しない', context => {
  expect(new VoiceReconnectPolicy().nextRetryDelayInMs(context)).toBeNull()
})
