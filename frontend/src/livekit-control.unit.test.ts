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
    protocol_version: '1.1',
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
    protocol_version: '1.1',
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

  test('受信event履歴の件数上限を超えない', () => {
    const receiver = new CoreEventReceiver(1, 1_048_576)
    receiver.receive(responseDelta('10000000-0000-4000-8000-000000000010', 1))

    expect(() => receiver.receive(
      responseDelta('10000000-0000-4000-8000-000000000011', 2),
    )).toThrow('capacity exceeded')
  })

  test('受信event履歴のbyte上限を超えない', () => {
    const first = responseDelta('10000000-0000-4000-8000-000000000010', 1)
    const receiver = new CoreEventReceiver(10, first.byteLength)
    receiver.receive(first)

    expect(() => receiver.receive(
      responseDelta('10000000-0000-4000-8000-000000000011', 2),
    )).toThrow('capacity exceeded')
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
    const createdConfirmation = tracker.create(
      '30000000-0000-4000-8000-000000000010',
      0,
    )

    expect(createdConfirmation?.event).toMatchObject({
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
  test('初回publish失敗時にentryとbyte counterを巻き戻す', async () => {
    vi.useFakeTimers()
    const outbox = new BrowserControlOutbox(
      async () => { throw new Error('publish failed') },
      vi.fn(),
      retryTimer,
    )

    await expect(outbox.enqueue(
      confirmation('event-1'),
      new Uint8Array([1, 2, 3]),
    )).rejects.toThrow('publish failed')

    expect(outbox.eventCount).toBe(0)
    expect(outbox.byteCount).toBe(0)
  })

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


test('途中prefixの通知後にも全出力確認を一度だけ送る', () => {
  let sequence = 20
  const tracker = new PlaybackConfirmationTracker(
    '20000000-0000-4000-8000-000000000010', () => 1000,
    () => `10000000-0000-4000-8000-${String(sequence++).padStart(12, '0')}`,
  )
  const responseId = '30000000-0000-4000-8000-000000000010'
  expect(tracker.create(responseId, 1)?.event.response_finished).toBeUndefined()
  const complete = tracker.create(responseId, 1, true)
  expect(complete?.event).toMatchObject({type: 'playback_completed', last_played_audio_sequence: 2, response_finished: true})
  expect(tracker.create(responseId, 1, true)).toBeNull()
  expect(tracker.create(responseId, 1)).toBeNull()
})

test('完全再生の音切れ実測値を通知し、途中prefixへの添付を拒否する', () => {
  const tracker = new PlaybackConfirmationTracker(
    '20000000-0000-4000-8000-000000000010', () => 1300,
    () => '10000000-0000-4000-8000-000000000020',
  )
  const responseId = '30000000-0000-4000-8000-000000000010'
  const summary = {expectedSamples: 2880, inputSamples: 1900, paddingSamples: 980,
    renderedSamples: 2880, packetCount: 3, firstOutputFrame: 48000,
    lastOutputEndFrame: 51008, gapSamples: 128, maximumGapSamples: 128, gapCount: 1,
    firstRtpTimestamp: 1000, lastRtpTimestamp: 2920, outputClockContextTime: 1.1,
    outputClockPerformanceTime: 1200, confirmationObservedAtMs: 1210, sampleRate: 48000 as const}
  expect(() => tracker.create(responseId, 1, false, summary)).toThrow('requires full completion')
  const complete = tracker.create(responseId, 1, true, summary)
  expect(complete?.event.playback_summary).toEqual({
    expected_samples: summary.expectedSamples,
    input_samples: summary.inputSamples,
    padding_samples: summary.paddingSamples,
    rendered_samples: summary.renderedSamples,
    packet_count: summary.packetCount,
    first_output_frame: summary.firstOutputFrame,
    last_output_end_frame: summary.lastOutputEndFrame,
    gap_samples: summary.gapSamples,
    maximum_gap_samples: summary.maximumGapSamples,
    gap_count: summary.gapCount,
    first_rtp_timestamp: summary.firstRtpTimestamp,
    last_rtp_timestamp: summary.lastRtpTimestamp,
    output_clock_context_time: summary.outputClockContextTime,
    output_clock_performance_time: summary.outputClockPerformanceTime,
    confirmation_observed_at_ms: summary.confirmationObservedAtMs,
    sample_rate: summary.sampleRate,
  })
  expect(tracker.create(responseId, 1, true, summary)).toBeNull()
})
