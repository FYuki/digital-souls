import {afterEach, beforeEach, expect, test, vi} from 'vitest'
import {CoreAckOutbox} from './livekit/core-ack-outbox'

const timer = {
  now: () => Date.now(),
  schedule: (callback: () => void, delay: number) => setTimeout(callback, delay),
  cancel: (handle: ReturnType<typeof setTimeout>) => clearTimeout(handle),
}
beforeEach(() => {vi.useFakeTimers(); vi.setSystemTime(0)})
afterEach(() => vi.useRealTimers())

test('ACK送信失敗は同じIDに集約して再送し、成功後は再送しない', async () => {
  const send = vi.fn().mockRejectedValueOnce(new Error('disconnected')).mockResolvedValue(undefined)
  const expired = vi.fn(), deferred = vi.fn()
  const outbox = new CoreAckOutbox(send, timer, expired, deferred)
  outbox.enqueue('event-1'); outbox.enqueue('event-1')
  await vi.advanceTimersByTimeAsync(0)
  expect(send).toHaveBeenCalledTimes(1)
  expect(deferred).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(250)
  expect(send.mock.calls).toEqual([['event-1'], ['event-1']])
  await vi.advanceTimersByTimeAsync(1000)
  expect(send).toHaveBeenCalledTimes(2)
  expect(expired).not.toHaveBeenCalled()
  expect(vi.getTimerCount()).toBe(0)
})

test('切断が60秒継続すると期限切れを一度通知して再送を止める', async () => {
  const send = vi.fn().mockRejectedValue(new Error('disconnected')), expired = vi.fn()
  const outbox = new CoreAckOutbox(send, timer, expired)
  outbox.enqueue('event-1')
  await vi.advanceTimersByTimeAsync(60000)
  expect(expired).toHaveBeenCalledTimes(1)
  const count = send.mock.calls.length
  await vi.advanceTimersByTimeAsync(1000)
  expect(send).toHaveBeenCalledTimes(count)
  expect(vi.getTimerCount()).toBe(0)
})

test('未完了のACKを256件に制限し、本文を追加保持しない', () => {
  const outbox = new CoreAckOutbox(() => new Promise<void>(() => undefined), timer, vi.fn())
  for (let index = 0; index < 256; index++) outbox.enqueue(`event-${index}`)
  expect(() => outbox.enqueue('event-256')).toThrow('capacity')
  expect(() => outbox.enqueue('event-0')).not.toThrow()
  outbox.clear()
})

test.each(['resolve', 'reject'])('clear前の送信完了は新しいキューを変更しない: %s', async mode => {
  let resolve!: () => void, reject!: (error: Error) => void
  const old = new Promise<void>((yes, no) => {resolve = yes; reject = no})
  const send = vi.fn().mockReturnValueOnce(old).mockRejectedValueOnce(new Error('retry')).mockResolvedValue(undefined)
  const deferred = vi.fn(), outbox = new CoreAckOutbox(send, timer, vi.fn(), deferred)
  outbox.enqueue('same-event')
  outbox.clear()
  outbox.enqueue('same-event')
  await vi.advanceTimersByTimeAsync(0)
  if (mode === 'resolve') resolve(); else reject(new Error('old failure'))
  await vi.advanceTimersByTimeAsync(250)
  expect(send).toHaveBeenCalledTimes(3)
  expect(deferred).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('clear後の送信失敗は再送を再開しない', async () => {
  let reject!: (error: Error) => void
  const send = vi.fn(() => new Promise<void>((_, no) => {reject = no}))
  const deferred = vi.fn(), outbox = new CoreAckOutbox(send, timer, vi.fn(), deferred)
  outbox.enqueue('event-1'); outbox.clear(); reject(new Error('closed'))
  await vi.advanceTimersByTimeAsync(1000)
  expect(send).toHaveBeenCalledTimes(1)
  expect(deferred).not.toHaveBeenCalled()
  expect(vi.getTimerCount()).toBe(0)
})

test.each([NaN, Infinity, -1])('無効な初期時計を拒否する: %s', now => {
  const send = vi.fn(), outbox = new CoreAckOutbox(send, {...timer, now: () => now}, vi.fn())
  expect(() => outbox.enqueue('event-1')).toThrow('clock invalid')
  expect(send).not.toHaveBeenCalled()
})
