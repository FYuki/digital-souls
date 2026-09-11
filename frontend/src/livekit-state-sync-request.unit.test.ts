import {afterEach, beforeEach, expect, test, vi} from 'vitest'
import {StateSyncRequest} from './livekit/state-sync-request'
const timer = {now: () => Date.now(), schedule: (fn: () => void, ms: number) => setTimeout(fn, ms),
  cancel: (handle: ReturnType<typeof setTimeout>) => clearTimeout(handle)}
beforeEach(() => {vi.useFakeTimers(); vi.setSystemTime(0)})
afterEach(() => vi.useRealTimers())

test('publish成功でも応答が来るまで再送し、確認後は停止する', async () => {
  const send = vi.fn().mockResolvedValue(undefined), expired = vi.fn()
  const request = new StateSyncRequest(send, timer, expired)
  request.start(); await vi.advanceTimersByTimeAsync(500)
  expect(send).toHaveBeenCalledTimes(3)
  request.close(); await vi.advanceTimersByTimeAsync(1000)
  expect(send).toHaveBeenCalledTimes(3)
  expect(expired).not.toHaveBeenCalled()
  expect(vi.getTimerCount()).toBe(0)
})

test('送信失敗後も再送し、送信が固まった場合は60秒で終了する', async () => {
  const send = vi.fn().mockRejectedValueOnce(new Error('disconnected'))
    .mockImplementation(() => new Promise<void>(() => undefined)), expired = vi.fn()
  const request = new StateSyncRequest(send, timer, expired)
  request.start(); await vi.advanceTimersByTimeAsync(60000)
  expect(send).toHaveBeenCalledTimes(2)
  expect(expired).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('開始直後に閉じた要求は送信しない', async () => {
  const send = vi.fn(), request = new StateSyncRequest(send, timer, vi.fn())
  request.start(); request.close(); await vi.advanceTimersByTimeAsync(1000)
  expect(send).not.toHaveBeenCalled()
})
