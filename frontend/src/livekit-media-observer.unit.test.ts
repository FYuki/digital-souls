import { ReadableStream, TransformStream, WritableStream } from 'node:stream/web'
import { afterEach, describe, expect, test, vi } from 'vitest'
import { encodedObserverWorkerSource, RemoteMediaObserver, type MediaObservation } from './livekit/media-observer'

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('受信とdecodeの独立観測', () => {
  test.each([true, false])('encoded frameを変更せず通し、worker clockをwindow clockへ写す（metadata=%s）', async metadataAvailable => {
    const frames = [{ data: new Uint8Array([1, 2, 3]).buffer, getMetadata: () => {
      if (!metadataAvailable) throw new Error('metadata unavailable')
      return { receiveTime: 11, rtpTimestamp: 9, synchronizationSource: 7 }
    } }, { data: new ArrayBuffer(4) }]
    const delivered: unknown[] = []
    const reported: unknown[] = []
    const worker: { onrtctransform?: (event: unknown) => void; postMessage: (value: unknown) => void } = {
      postMessage: (value) => reported.push(value),
    }
    new Function('self', 'performance', 'TransformStream', encodedObserverWorkerSource)(
      worker, { timeOrigin: 2000, now: () => 17 }, TransformStream,
    )
    let completed!: () => void
    const done = new Promise<void>((resolve) => { completed = resolve })
    worker.onrtctransform?.({ transformer: {
      options: { windowTimeOrigin: 1000 },
      readable: new ReadableStream({ start(controller) {
        for (const frame of frames) controller.enqueue(frame)
        controller.close()
      } }),
      writable: new WritableStream({ write(frame) { delivered.push(frame) }, close() { completed() } }),
    } })
    await done
    expect(delivered[0]).toBe(frames[0])
    expect(delivered[1]).toBe(frames[1])
    expect(reported).toEqual([{ kind: 'encoded', atMs: 1017, ...(metadataAvailable
      ? { packet: { receivedAtMs: 1011, rtpTimestamp: 9, source: 7 } } : {}) }])
  })

  test('未対応APIは欠測理由を返し、時刻を捏造しない', () => {
    vi.stubGlobal('RTCRtpScriptTransform', undefined)
    vi.stubGlobal('MediaStreamTrackProcessor', undefined)
    const observed: MediaObservation[] = []
    const observer = new RemoteMediaObserver(undefined, {} as MediaStreamTrack, (value) => observed.push(value))
    expect(observed.at(-1)).toMatchObject({
      encodedMissingReason: 'api_unavailable', decodedMissingReason: 'api_unavailable',
    })
    expect(observer.snapshot().firstEncodedFrameAtMs).toBeUndefined()
    expect(observer.snapshot().firstNonzeroDecodedFrameAtMs).toBeUndefined()
    observer.close()
  })

  test('無音をdecode開始にせず、frameとcloneを解放して元trackを維持する', async () => {
    vi.stubGlobal('RTCRtpScriptTransform', undefined)
    const originalStop = vi.fn()
    const cloneStop = vi.fn()
    const clone = { stop: cloneStop }
    const frameClose = [vi.fn(), vi.fn()]
    const frames = [0, .25].map((sample, index) => ({
      numberOfFrames: 4, numberOfChannels: 1, timestamp: 900000000000,
      copyTo: (target: Float32Array) => target.fill(sample), close: frameClose[index],
    }))
    vi.stubGlobal('MediaStreamTrackProcessor', class {
      readable = new ReadableStream({ start(controller) {
        for (const frame of frames) controller.enqueue(frame)
        controller.close()
      } })
      constructor(options: { track: unknown }) { expect(options.track).toBe(clone) }
    })
    const observed: MediaObservation[] = []
    const observer = new RemoteMediaObserver(undefined, {
      stop: originalStop, clone: () => clone,
    } as unknown as MediaStreamTrack, (value) => observed.push(value))
    await vi.waitFor(() => expect(observer.snapshot().firstNonzeroDecodedFrameAtMs).toBeDefined())
    expect(observed.filter((value) => value.firstNonzeroDecodedFrameAtMs !== undefined)).toHaveLength(1)
    expect(observer.snapshot().firstNonzeroDecodedFrameAtMs).toBeLessThan(900000000000)
    for (const close of frameClose) expect(close).toHaveBeenCalledOnce()
    expect(cloneStop).toHaveBeenCalledOnce()
    observer.close()
    expect(originalStop).not.toHaveBeenCalled()
    expect(cloneStop).toHaveBeenCalledOnce()
  })
})

test('encoded観測の失敗時にtransformを外し、元の受信経路へ戻す', () => {
  const originalUrl = URL
  const terminate = vi.fn()
  let worker: { onmessage?: (event: { data: { kind: string } }) => void } | undefined
  vi.stubGlobal('URL', class extends originalUrl {
    static createObjectURL = vi.fn(() => 'blob:observer-test')
    static revokeObjectURL = vi.fn()
  })
  vi.stubGlobal('Worker', class {
    onmessage?: (event: { data: { kind: string } }) => void
    terminate = terminate
    constructor() { worker = this }
  })
  vi.stubGlobal('RTCRtpScriptTransform', class {})
  vi.stubGlobal('MediaStreamTrackProcessor', undefined)
  const receiver = { transform: null } as unknown as RTCRtpReceiver
  const observer = new RemoteMediaObserver(receiver, {} as MediaStreamTrack, () => undefined)
  expect(receiver.transform).not.toBeNull()
  worker?.onmessage?.({ data: { kind: 'error' } })
  expect(receiver.transform).toBeNull()
  expect(terminate).toHaveBeenCalledOnce()
  expect(observer.snapshot().encodedMissingReason).toBe('observer_failed')
  observer.close()
})

test('既存のtransformを上書きせず、欠測理由を明示する', () => {
  vi.stubGlobal('RTCRtpScriptTransform', class {})
  vi.stubGlobal('Worker', class {})
  vi.stubGlobal('MediaStreamTrackProcessor', undefined)
  const existing = {} as RTCRtpScriptTransform
  const receiver = { transform: existing } as RTCRtpReceiver
  const observer = new RemoteMediaObserver(receiver, {} as MediaStreamTrack, () => undefined)
  expect(observer.snapshot().encodedMissingReason).toBe('transform_already_in_use')
  observer.close()
  expect(receiver.transform).toBe(existing)
})


type SyncSource = { source: number; rtpTimestamp: number; timestamp: number }
type PacketMessage = { kind: string; atMs: number; packet?: { source: number; rtpTimestamp: number; receivedAtMs: number } }
const packetObserver = (initial: SyncSource[] = []) => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
  let now = 300
  let sources = initial
  let worker!: { onmessage?: (event: { data: PacketMessage }) => void }
  const originalUrl = URL
  vi.stubGlobal('performance', { timeOrigin: 1000, now: () => now })
  vi.stubGlobal('URL', class extends originalUrl {
    static createObjectURL = vi.fn(() => 'blob:packet-observer')
    static revokeObjectURL = vi.fn()
  })
  vi.stubGlobal('Worker', class {
    onmessage?: (event: { data: PacketMessage }) => void
    terminate = vi.fn()
    constructor() { worker = this }
  })
  vi.stubGlobal('RTCRtpScriptTransform', class {})
  vi.stubGlobal('MediaStreamTrackProcessor', undefined)
  const receiver = { transform: null, getSynchronizationSources: () => sources } as unknown as RTCRtpReceiver
  const observer = new RemoteMediaObserver(receiver, {} as MediaStreamTrack, () => undefined)
  const send = (rtpTimestamp: number, source: number, receivedAtMs = 100) => {
    worker.onmessage?.({ data: { kind: 'encoded', atMs: now, packet: { rtpTimestamp, source, receivedAtMs } } })
  }
  return { observer, send, setSources: (next: SyncSource[]) => { sources = next },
    advance: (ms: number) => { now += ms; vi.advanceTimersByTime(ms) } }
}

describe('同一RTP frameの受信とトラック配送', () => {
  test('配送がworker通知より先でもbufferから同じframeを照合する', () => {
    const p = packetObserver([{ source: 7, rtpTimestamp: 9, timestamp: 1200 }])
    p.setSources([{ source: 7, rtpTimestamp: 10, timestamp: 1210 }]); p.advance(2)
    p.send(9, 7)
    expect(p.observer.snapshot()).toMatchObject({ firstPacketReceivedAtMs: 100, firstPacketDeliveredAtMs: 200 })
    expect(p.observer.snapshot().packetDeliveryMissingReason).toBeUndefined()
    expect(vi.getTimerCount()).toBe(0)
    p.observer.close()
  })

  test('RTP timestampだけ一致する別送信元を同じframeにしない', () => {
    const p = packetObserver([{ source: 8, rtpTimestamp: 9, timestamp: 1200 }])
    p.send(9, 7)
    expect(p.observer.snapshot().firstPacketDeliveredAtMs).toBeUndefined()
    p.advance(2000)
    expect(p.observer.snapshot().packetDeliveryMissingReason).toBe('matching_packet_not_observed')
    expect(p.observer.snapshot().firstPacketReceivedAtMs).toBe(100)
    p.observer.close()
  })

  test('RTP timestampの0を欠測と取り違えない', () => {
    const p = packetObserver([{ source: 7, rtpTimestamp: 0, timestamp: 1200 }])
    p.send(0, 7)
    expect(p.observer.snapshot().firstPacketDeliveredAtMs).toBe(200)
    p.observer.close()
  })

  test.each([900, 1400])('受信より前・観測より未来の配送clockを採用しない: %s', timestamp => {
    const p = packetObserver([{ source: 7, rtpTimestamp: 9, timestamp }])
    p.send(9, 7)
    expect(p.observer.snapshot().firstPacketDeliveredAtMs).toBeUndefined()
    expect(p.observer.snapshot().packetDeliveryMissingReason).toBe('packet_clock_invalid')
    expect(vi.getTimerCount()).toBe(0)
    p.observer.close()
  })

  test('無音で初期窓が終わっても、最初のpacket到着後に同じframeを観測する', () => {
    const p = packetObserver()
    p.advance(2500)
    p.setSources([{ source: 7, rtpTimestamp: 9, timestamp: 3780 }])
    p.send(9, 7, 2770)
    expect(p.observer.snapshot()).toMatchObject({ firstPacketReceivedAtMs: 2770, firstPacketDeliveredAtMs: 2780 })
    expect(p.observer.snapshot().packetDeliveryMissingReason).toBeUndefined()
    p.observer.close()
  })

  test('bufferから失われた最初のframeを後続frameで補完しない', () => {
    const p = packetObserver()
    for (let index = 0; index < 140; index++) {
      p.setSources([{ source: 7, rtpTimestamp: index, timestamp: 1200 + index }]); p.advance(2)
    }
    p.send(0, 7)
    p.advance(2000)
    expect(p.observer.snapshot().firstPacketDeliveredAtMs).toBeUndefined()
    expect(p.observer.snapshot().packetDeliveryMissingReason).toBe('matching_packet_not_observed')
    p.observer.close()
  })

  test('終了後はpollも遅れて届いた通知も観測を増やさない', () => {
    const p = packetObserver()
    p.observer.close()
    const closed = p.observer.snapshot()
    p.setSources([{ source: 7, rtpTimestamp: 9, timestamp: 1200 }]); p.send(9, 7); p.advance(2000)
    expect(p.observer.snapshot()).toEqual(closed)
    expect(vi.getTimerCount()).toBe(0)
  })
})
