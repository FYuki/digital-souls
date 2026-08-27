import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  BrowserControlOutbox,
  CoreEventReceiver,
  PlaybackConfirmationTracker,
  type PlaybackConfirmation,
  type RetryTimer,
} from './livekit/control'

const retryTimer: RetryTimer = {
  now: () => Date.now(),
  schedule: (callback, delayMs) => setTimeout(callback, delayMs),
  cancel: (handle) => clearTimeout(handle),
}

const confirmation = (eventId: string): PlaybackConfirmation => ({
  event: {
    protocol_version: '1.0',
    event_id: eventId,
    type: 'playback_completed',
    session_id: '20000000-0000-4000-8000-000000000010',
    response_id: '30000000-0000-4000-8000-000000000010',
    last_played_audio_sequence: 1,
    monotonic_timestamp_ms: 1_234,
  },
  responseId: '30000000-0000-4000-8000-000000000010',
  continuousPrefix: 0,
})

afterEach(() => {
  vi.useRealTimers()
})

const responseDelta = (eventId: string, sequence: number): Uint8Array => new TextEncoder().encode(
  JSON.stringify({
    protocol_version: '1.0',
    event_id: eventId,
    type: 'response_delta',
    session_id: '20000000-0000-4000-8000-000000000010',
    response_id: '30000000-0000-4000-8000-000000000010',
    text_sequence: sequence,
    text: 'a',
    text_range: { start: sequence - 1, end: sequence },
    monotonic_timestamp_ms: sequence,
  }),
)

describe('LiveKit Core event receiver', () => {
  test('同一payloadの再送だけをduplicateとして受理する', () => {
    const receiver = new CoreEventReceiver()
    const payload = responseDelta('10000000-0000-4000-8000-000000000010', 1)

    expect(receiver.receive(payload).duplicate).toBe(false)
    expect(receiver.receive(payload).duplicate).toBe(true)
    expect(() => receiver.receive(
      responseDelta('10000000-0000-4000-8000-000000000010', 2),
    )).toThrow('conflicting payload')
  })

  test('欠番をCore consumerへ渡す前に拒否する', () => {
    const receiver = new CoreEventReceiver()

    expect(() => receiver.receive(
      responseDelta('10000000-0000-4000-8000-000000000011', 2),
    )).toThrow('text_sequence must be contiguous')
  })
})

describe('LiveKit playback confirmation', () => {
  test('0始まりのrender済みprefixをCore sequenceへ変換する', () => {
    const tracker = new PlaybackConfirmationTracker(
      '20000000-0000-4000-8000-000000000010',
      () => 1_234,
      () => '10000000-0000-4000-8000-000000000020',
    )

    expect(tracker.create('30000000-0000-4000-8000-000000000010', -1)).toBeNull()
    const confirmation = tracker.create(
      '30000000-0000-4000-8000-000000000010',
      0,
    )

    expect(confirmation?.event).toMatchObject({
      type: 'playback_completed',
      session_id: '20000000-0000-4000-8000-000000000010',
      response_id: '30000000-0000-4000-8000-000000000010',
      last_played_audio_sequence: 1,
      monotonic_timestamp_ms: 1_234,
    })
    expect(tracker.create('30000000-0000-4000-8000-000000000010', 0)).toBeNull()
  })
})

describe('LiveKit browser control outbox', () => {
  test('同じpayloadを1秒、2秒、4秒後に再送して枯渇を通知する', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(10_000)
    const published: Uint8Array<ArrayBuffer>[] = []
    const publishedAt: number[] = []
    const unavailable = vi.fn()
    const outbox = new BrowserControlOutbox(async (payload) => {
      published.push(payload)
      publishedAt.push(Date.now())
    }, unavailable, retryTimer)
    const payload = new Uint8Array([1, 2, 3])

    await outbox.enqueue(confirmation('event-1'), payload)
    payload[0] = 9
    expect(published.map((value) => [...value])).toEqual([[1, 2, 3]])

    await vi.advanceTimersByTimeAsync(999)
    expect(published).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(published).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(999)
    expect(published).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(published).toHaveLength(3)
    await vi.advanceTimersByTimeAsync(1_999)
    expect(published).toHaveLength(3)
    await vi.advanceTimersByTimeAsync(1)

    expect(published).toHaveLength(4)
    expect(publishedAt).toEqual([10_000, 11_000, 12_000, 14_000])
    expect(published.map((value) => [...value])).toEqual([
      [1, 2, 3],
      [1, 2, 3],
      [1, 2, 3],
      [1, 2, 3],
    ])
    expect(unavailable).toHaveBeenCalledTimes(1)
  })

  test('ACK後はentryとtimerを除去して後続再送を止める', async () => {
    vi.useFakeTimers()
    const publish = vi.fn(async (_payload: Uint8Array<ArrayBuffer>) => undefined)
    const unavailable = vi.fn()
    const outbox = new BrowserControlOutbox(publish, unavailable, retryTimer)
    const expected = confirmation('event-1')

    await outbox.enqueue(expected, new Uint8Array([1]))
    await vi.advanceTimersByTimeAsync(1_000)
    expect(outbox.acknowledge('event-1')).toEqual(expected)
    expect(outbox.acknowledge('event-1')).toBeNull()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(publish).toHaveBeenCalledTimes(2)
    expect(unavailable).not.toHaveBeenCalled()
    expect(outbox.eventCount).toBe(0)
    expect(outbox.byteCount).toBe(0)
  })

  test('256 eventちょうどを保持し257件目を明示的に拒否する', async () => {
    vi.useFakeTimers()
    const unavailable = vi.fn()
    const outbox = new BrowserControlOutbox(
      async (_payload) => undefined,
      unavailable,
      retryTimer,
    )

    for (let index = 0; index < 256; index += 1) {
      await outbox.enqueue(confirmation(`event-${index}`), new Uint8Array([1]))
    }

    await expect(outbox.enqueue(
      confirmation('event-overflow'),
      new Uint8Array([1]),
    )).rejects.toThrow('outbox capacity exceeded')
    expect(outbox.eventCount).toBe(256)
    expect(outbox.acknowledge('event-0')).not.toBeNull()
    expect(unavailable).toHaveBeenCalledTimes(1)
  })

  test('1 MiBちょうどを保持し追加1 byteを明示的に拒否する', async () => {
    vi.useFakeTimers()
    const unavailable = vi.fn()
    const outbox = new BrowserControlOutbox(
      async (_payload) => undefined,
      unavailable,
      retryTimer,
    )

    await outbox.enqueue(confirmation('event-1'), new Uint8Array(1_048_576))

    await expect(outbox.enqueue(
      confirmation('event-overflow'),
      new Uint8Array([1]),
    )).rejects.toThrow('outbox capacity exceeded')
    expect(outbox.byteCount).toBe(1_048_576)
    expect(unavailable).toHaveBeenCalledTimes(1)
  })

  test('clearは全entryとtimerを破棄して再送を止める', async () => {
    vi.useFakeTimers()
    const publish = vi.fn(async (_payload: Uint8Array<ArrayBuffer>) => undefined)
    const unavailable = vi.fn()
    const outbox = new BrowserControlOutbox(publish, unavailable, retryTimer)
    await outbox.enqueue(confirmation('event-1'), new Uint8Array([1]))

    outbox.clear()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(publish).toHaveBeenCalledTimes(1)
    expect(unavailable).not.toHaveBeenCalled()
    expect(outbox.eventCount).toBe(0)
    expect(outbox.byteCount).toBe(0)
  })
})
