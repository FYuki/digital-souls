import { ReadableStream, TransformStream, WritableStream } from 'node:stream/web'
import { afterEach, describe, expect, test, vi } from 'vitest'
import { encodedObserverWorkerSource, RemoteMediaObserver, type MediaObservation } from './livekit/media-observer'

afterEach(() => vi.unstubAllGlobals())

describe('受信とdecodeの独立観測', () => {
  test('encoded frameを変更せず通し、worker clockをwindow clockへ写す', async () => {
    const frames = [{ data: new Uint8Array([1, 2, 3]).buffer }, { data: new ArrayBuffer(4) }]
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
    expect(reported).toEqual([{ kind: 'encoded', atMs: 1017 }])
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
    expect(observer.snapshot().firstDecodedSampleAtMs).toBeUndefined()
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
    await vi.waitFor(() => expect(observer.snapshot().firstDecodedSampleAtMs).toBeDefined())
    expect(observed.filter((value) => value.firstDecodedSampleAtMs !== undefined)).toHaveLength(1)
    expect(observer.snapshot().firstDecodedSampleAtMs).toBeLessThan(900000000000)
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
