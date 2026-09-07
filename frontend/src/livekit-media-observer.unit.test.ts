import { ReadableStream, TransformStream, WritableStream } from 'node:stream/web'
import { afterEach, describe, expect, test, vi } from 'vitest'
import { encodedObserverWorkerSource, RemoteMediaObserver, type MediaObservation } from './livekit/media-observer'

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('受信とdecodeの独立観測', () => {
  test.each([true, false])('encoded frameを変更せず通し、epoch換算なしでworkerの生時刻を通知する（metadata=%s）', async metadataAvailable => {
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
      worker, { get timeOrigin() { throw new Error('epoch clock must not be read') }, now: () => 17 }, TransformStream,
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
    expect(reported).toEqual([{ kind: 'encoded', workerAtMs: 17, ...(metadataAvailable
      ? { packet: { receivedAtWorkerMs: 11, rtpTimestamp: 9, source: 7 } } : {}) }])
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
    postMessage = vi.fn()
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
type PacketMessage = { kind: string; workerAtMs?: number; sequence?: number; packet?: { source: number; rtpTimestamp: number; receivedAtWorkerMs: number }; packetIndex?: number; samples?: number; reason?: string }
const packetObserver = (initial: SyncSource[] = [], autoClock = true) => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'setTimeout', 'clearTimeout'] })
  let now = 300
  let sources = initial
  const clockRequests: {kind: string; sequence: number}[] = []
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
    postMessage(message: {kind: string; sequence: number}) {
      clockRequests.push(message)
      if (autoClock) this.onmessage?.({data: {kind: 'clock', sequence: message.sequence, workerAtMs: now}})
    }
    constructor() { worker = this }
  })
  vi.stubGlobal('RTCRtpScriptTransform', class {})
  vi.stubGlobal('MediaStreamTrackProcessor', undefined)
  const receiver = { transform: null, getSynchronizationSources: () => sources } as unknown as RTCRtpReceiver
  const observer = new RemoteMediaObserver(receiver, {} as MediaStreamTrack, () => undefined)
  const send = (rtpTimestamp: number, source: number, receivedAtMs = 100) => {
    worker.onmessage?.({ data: { kind: 'encoded', workerAtMs: now, packet: { rtpTimestamp, source, receivedAtWorkerMs: receivedAtMs } } })
  }
  return { observer, send,
    decoded: (overrides: Partial<PacketMessage> = {}) => worker.onmessage?.({data: {
      kind: 'packet_decoded', packetIndex: 0, samples: 960, workerAtMs: 110,
      packet: {source: 7, rtpTimestamp: 9, receivedAtWorkerMs: 100}, ...overrides,
    }}),
    calibrate: () => {
      for (let index = 0; index < 10; index++) {
        worker.onmessage?.({data: {kind: 'clock', sequence: clockRequests[index].sequence, workerAtMs: now}})
      }
    }, setSources: (next: SyncSource[]) => { sources = next },
    advance: (ms: number) => { now += ms; vi.advanceTimersByTime(ms) } }
}

describe('同一RTP frameの受信とトラック配送', () => {
  test('配送がworker通知より先でもbufferから同じframeを照合する', () => {
    const p = packetObserver([{ source: 7, rtpTimestamp: 9, timestamp: 1200 }])
    p.setSources([{ source: 7, rtpTimestamp: 10, timestamp: 1210 }]); p.advance(2)
    p.send(9, 7)
    expect(p.observer.snapshot()).toMatchObject({ firstPacketReceivedAtMs: 99.8, firstPacketDeliveredAtMs: 200 })
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
    expect(p.observer.snapshot().firstPacketReceivedAtMs).toBe(99.8)
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
    expect(p.observer.snapshot()).toMatchObject({ firstPacketReceivedAtMs: 2769.8, firstPacketDeliveredAtMs: 2780 })
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


describe('worker時計の較正と計測待ち', () => {
  test('較正前のpacketを保留し、較正後に受信時刻の上下限を記録する', () => {
    const p = packetObserver([{source: 7, rtpTimestamp: 9, timestamp: 1200}], false)
    p.send(9, 7)
    expect(p.observer.snapshot().firstPacketReceivedAtMs).toBeUndefined()
    p.calibrate()
    expect(p.observer.snapshot()).toMatchObject({
      workerClockMethod: 'causal_message_bounds',
      workerClockOffsetBoundsMs: {lowerMs: -0.2, upperMs: 0.2},
      firstPacketReceivedAtBoundsMs: {lowerMs: 99.8, upperMs: 100.2},
      firstPacketReceivedAtMs: 99.8, firstPacketDeliveredAtMs: 200,
    })
    expect(vi.getTimerCount()).toBe(0)
    p.observer.close()
  })

  test('較正timeoutで受信時刻を捏造せず、終了後の通知を無視する', () => {
    const p = packetObserver([], false)
    p.send(9, 7)
    p.advance(1000)
    expect(p.observer.snapshot().encodedMissingReason).toBe('clock_calibration_failed')
    expect(p.observer.snapshot().firstPacketReceivedAtMs).toBeUndefined()
    expect(vi.getTimerCount()).toBe(0)
    p.observer.close()
  })
})


test('同一packetの独立decodeは受信時刻と別に較正し、較正前の通知も保持する', () => {
  const p = packetObserver([], false)
  p.decoded()
  p.send(9, 7)
  expect(p.observer.snapshot().firstPacketDecodedAtMs).toBeUndefined()
  p.calibrate()
  expect(p.observer.snapshot()).toMatchObject({
    firstPacketReceivedAtMs: 99.8, firstPacketDecodedAtMs: 109.8,
    firstPacketDecodedAtBoundsMs: {lowerMs: 109.8, upperMs: 110.2}, firstPacketDecodedSamples: 960,
  })
  p.observer.close()
})

test.each(['wrong_source', 'wrong_packet', 'wrong_count', 'wrong_index', 'early', 'future'])('誤ったpacketや時計を相関済みdecodeへしない（%s）', damage => {
  const p = packetObserver()
  p.send(9, 7)
  p.decoded(damage === 'wrong_source' ? {packet: {source: 8, rtpTimestamp: 9, receivedAtWorkerMs: 100}}
    : damage === 'wrong_packet' ? {packet: {source: 7, rtpTimestamp: 10, receivedAtWorkerMs: 100}}
      : damage === 'wrong_count' ? {samples: 480} : damage === 'wrong_index' ? {packetIndex: 1}
        : {workerAtMs: damage === 'early' ? 99 : 99999})
  expect(p.observer.snapshot().firstPacketDecodedAtMs).toBeUndefined()
  expect(p.observer.snapshot().packetDecodeMissingReason).toBeDefined()
  p.observer.close()
})

test('close後のdecode通知を無視する', () => {
  const p = packetObserver()
  p.send(9, 7)
  p.observer.close()
  p.decoded()
  expect(p.observer.snapshot().firstPacketDecodedAtMs).toBeUndefined()
})


test.each(['normal', 'overflow', 'duplicate'])('独立decodeの待機・重複・queue超過をnative frame配送と分離する（%s）', async mode => {
  const overflow = mode === 'overflow'
  const reported: {kind: string; packetIndex?: number; reason?: string}[] = []
  const delivered: unknown[] = []
  let output!: (frame: unknown) => void
  const requests: unknown[] = []
  const close = vi.fn()
  class Decoder {
    static isConfigSupported = async () => ({supported: true})
    state = 'configured'
    constructor(options: {output: typeof output}) {output = options.output}
    configure() {}
    decode(chunk: unknown) {requests.push(chunk)}
    close() {this.state = 'closed'; close()}
    flush() {throw new Error('flush must not be called')}
  }
  const count = overflow ? 60 : 2
  const frames = Array.from({length: count}, (_, index) => ({
    data: new Uint8Array([0x98, 1]).buffer,
    getMetadata: () => ({receiveTime: 1, rtpTimestamp: 9 + index * 960, synchronizationSource: 7, mimeType: 'audio/opus'}),
  }))
  if (mode === 'duplicate') frames.splice(1, 0, frames[0])
  const worker: {onrtctransform?: (event: unknown) => Promise<void>; postMessage: (row: typeof reported[number]) => void} = {
    postMessage: row => reported.push(row),
  }
  new Function('self', 'TransformStream', 'AudioDecoder', 'EncodedAudioChunk', encodedObserverWorkerSource)(
    worker, TransformStream, Decoder, class {},
  )
  await worker.onrtctransform?.({transformer: {
    options: {decodePackets: true, emitPcm: mode === 'duplicate'},
    readable: new ReadableStream({start(controller) {for (const frame of frames) controller.enqueue(frame); controller.close()}}),
    writable: new WritableStream({write(frame) {delivered.push(frame)}}),
  }})
  expect(delivered).toEqual(frames)
  expect(requests).toHaveLength(1)
  if (overflow) {
    expect(reported).toContainEqual({kind: 'packet_decode_error', reason: 'opus_decode_queue_overflow'})
  } else {
    for (let index = 0; index < count; index++) {
      output({numberOfFrames: 960, sampleRate: 48000, numberOfChannels: 1, timestamp: 999,
        copyTo: (samples: Float32Array) => samples.fill(.1), close: vi.fn()})
      await Promise.resolve()
    }
    expect(reported.filter(row => row.kind === 'packet_decoded')).toHaveLength(1)
    expect(requests).toHaveLength(count)
    if (mode === 'duplicate') {
      expect(reported).toContainEqual({kind: 'packet_duplicate', count: 1})
      expect(reported.filter(row => row.kind === 'pcm').map(row => row.packetIndex)).toEqual([0, 1])
    }
  }
  await vi.waitFor(() => expect(close).toHaveBeenCalledOnce())
})
