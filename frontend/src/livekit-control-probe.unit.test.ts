import { describe, expect, test, vi } from 'vitest'
import { ControlProbeTracker } from './livekit/control-probe'

const setup = () => {
  let now = 100
  let sequence = 0
  const jobs = new Map<unknown, () => void>()
  const timer = {
    now: () => now,
    schedule: (callback: () => void, delay: number) => {
      expect(delay).toBe(500)
      const key = {}; jobs.set(key, callback); return key
    },
    cancel: (key: unknown) => { jobs.delete(key) },
  }
  const tracker = new ControlProbeTracker(timer, () => `10000000-0000-4000-8000-${String(++sequence).padStart(12, '0')}`)
  const send = vi.fn(async (_id: string, _generation: number) => undefined)
  return {tracker, send, jobs, time: (value: number) => {now = value}}
}

describe('会話状態を変更しない制御probeの往復観測', () => {
  test('publish完了だけでは成功せず、同じnonceと世代のackで初めて時刻を確定する', async () => {
    const h = setup()
    const pending = h.tracker.start(2, h.send)
    let settled = false
    void pending.then(() => {settled = true})
    await Promise.resolve()
    expect(settled).toBe(false)
    h.time(135)
    h.tracker.acknowledge('10000000-0000-4000-8000-000000000999', 2)
    h.tracker.acknowledge(h.send.mock.calls[0][0], 1)
    await Promise.resolve()
    expect(settled).toBe(false)
    h.tracker.acknowledge(h.send.mock.calls[0][0], 2)
    expect(await pending).toMatchObject({status: 'received', sentAtMs: 100, receivedAtMs: 135, generation: 2})
    expect(h.jobs.size).toBe(0)
  })

  test('未応答は1件に限定し、timeout後の遅着ackを次の試行へ流用しない', async () => {
    const h = setup()
    const first = h.tracker.start(0, h.send)
    expect((await h.tracker.start(0, h.send)).status).toBe('busy')
    expect(h.send).toHaveBeenCalledTimes(1)
    const oldId = h.send.mock.calls[0][0]
    h.jobs.values().next().value!()
    expect(await first).toMatchObject({status: 'timeout', receivedAtMs: null})
    const second = h.tracker.start(0, h.send)
    h.tracker.acknowledge(oldId, 0)
    expect(h.jobs.size).toBe(1)
    h.tracker.reset()
    expect((await second).status).toBe('interrupted')
    expect(h.jobs.size).toBe(0)
  })

  test('切断後に戻る古い送信失敗で新しいprobeを終了しない', async () => {
    const h = setup()
    let reject!: (reason: Error) => void
    const first = h.tracker.start(1, async () => new Promise<void>((_, fail) => {reject = fail}))
    h.tracker.reset()
    expect((await first).status).toBe('interrupted')
    const second = h.tracker.start(2, h.send)
    reject(new Error('private provider detail'))
    await Promise.resolve(); await Promise.resolve()
    h.tracker.acknowledge(h.send.mock.calls[0][0], 2)
    expect((await second).status).toBe('received')
  })

  test.each(['sync', 'async'])('送信失敗を記録し、例外本文を観測へ保存しない: %s', async mode => {
    const h = setup()
    const send = mode === 'sync' ? () => {throw new Error('private')}
      : async () => {throw new Error('private')}
    const result = await h.tracker.start(0, send)
    expect(result.status).toBe('send_failed')
    expect(JSON.stringify(result)).not.toContain('private')
    expect(h.jobs.size).toBe(0)
  })

  test.each([NaN, Infinity, -1])('無効な送信時計では送らない: %s', async value => {
    const h = setup(); h.time(value)
    expect((await h.tracker.start(0, h.send)).status).toBe('clock_invalid')
    expect(h.send).not.toHaveBeenCalled()
  })

  test.each([NaN, Infinity, 99])('復旧時刻の逆転や非有限値を成功にしない: %s', async value => {
    const h = setup(); const pending = h.tracker.start(0, h.send); h.time(value)
    h.tracker.acknowledge(h.send.mock.calls[0][0], 0)
    expect(await pending).toMatchObject({status: 'clock_invalid', receivedAtMs: null})
  })

  test('timer callbackが遅延しても500msを過ぎたackを成功にしない', async () => {
    const h = setup(); const pending = h.tracker.start(0, h.send); h.time(600)
    h.tracker.acknowledge(h.send.mock.calls[0][0], 0)
    expect(await pending).toMatchObject({status: 'timeout', receivedAtMs: null})
  })
})
