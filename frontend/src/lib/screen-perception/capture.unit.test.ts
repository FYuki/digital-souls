import { beforeEach, describe, expect, test, vi } from 'vitest'

import type { SnapshotRequested } from './generated'
import {
  BrowserScreenCaptureController,
  SCREEN_CAPTURE_LIMITS,
  screenCaptureFailureReason,
  type ScreenCaptureState,
} from './capture'

const SCREEN_SESSION_ID = '10000000-0000-4000-8000-000000000001'
const CLIENT_SESSION_ID = '20000000-0000-4000-8000-000000000001'
const REQUEST_ID = '30000000-0000-4000-8000-000000000001'
const TURN_ID = '40000000-0000-4000-8000-000000000001'

class FakeTrack extends EventTarget {
  kind = 'video'
  label = '公開テスト画面'
  muted = false
  readyState: MediaStreamTrackState = 'live'
  stop = vi.fn(() => { this.readyState = 'ended' })

  constructor(private readonly displaySurface: string | null = 'monitor') {
    super()
  }

  getSettings(): MediaTrackSettings {
    return this.displaySurface === null ? {} : { displaySurface: this.displaySurface }
  }
}

const fakeStream = (track = new FakeTrack(), audioTracks: MediaStreamTrack[] = []) => ({
  getTracks: () => [track as unknown as MediaStreamTrack, ...audioTracks],
  getVideoTracks: () => [track as unknown as MediaStreamTrack],
  getAudioTracks: () => audioTracks,
}) as unknown as MediaStream

const snapshotRequest = (generation: number): SnapshotRequested => ({
  protocol_version: '1.0',
  type: 'screen_snapshot_requested',
  event_id: '50000000-0000-4000-8000-000000000001',
  screen_session_id: SCREEN_SESSION_ID,
  generation,
  request_id: REQUEST_ID,
  turn_id: TURN_ID,
  source: 'explicit_ui',
  requested_at: '2026-09-06T00:00:00.000Z',
  capture_deadline: '2026-09-06T00:00:05.000Z',
})

const createHarness = ({
  stream = fakeStream(),
  secure = true,
  active = true,
  api = true,
  pngSize = 128,
  jpegSize = 96,
  width = 1920,
  height = 1080,
  freshFrame = true,
  readyState = HTMLMediaElement.HAVE_CURRENT_DATA,
}: {
  stream?: MediaStream
  secure?: boolean
  active?: boolean
  api?: boolean
  pngSize?: number
  jpegSize?: number
  width?: number
  height?: number
  freshFrame?: boolean
  readyState?: number
} = {}) => {
  const states: ScreenCaptureState[] = []
  const getDisplayMedia = vi.fn(() => Promise.resolve(stream))
  const drawImage = vi.fn()
  const canvas = {
    width: 0,
    height: 0,
    getContext: vi.fn(() => ({ drawImage })),
    toBlob: vi.fn((callback: BlobCallback, type?: string) => {
      const size = type === 'image/jpeg' ? jpegSize : pngSize
      callback(new Blob([new Uint8Array(size)], { type }))
    }),
  } as unknown as HTMLCanvasElement
  const preview = document.createElement('video')
  Object.defineProperties(preview, {
    videoWidth: { configurable: true, value: width },
    videoHeight: { configurable: true, value: height },
    readyState: { configurable: true, value: readyState },
    play: { configurable: true, value: vi.fn(async () => undefined) },
    pause: { configurable: true, value: vi.fn() },
    requestVideoFrameCallback: {
      configurable: true,
      value: freshFrame ? vi.fn((callback: () => void) => { callback(); return 1 }) : undefined,
    },
  })
  const controller = new BrowserScreenCaptureController(preview, (state) => states.push(state), {
    isSecureContext: () => secure,
    hasTransientActivation: () => active,
    getDisplayMedia: api ? getDisplayMedia : null,
    createCanvas: () => canvas,
    createUuid: vi.fn()
      .mockReturnValueOnce('60000000-0000-4000-8000-000000000001')
      .mockReturnValueOnce('70000000-0000-4000-8000-000000000001'),
    now: () => new Date('2026-09-06T00:00:01.000Z'),
    setTimer: (callback, delay) => window.setTimeout(callback, delay),
    clearTimer: (handle) => window.clearTimeout(handle),
  })
  return { controller, preview, states, getDisplayMedia, canvas, drawImage }
}

describe('BrowserScreenCaptureController', () => {
  beforeEach(() => vi.useRealTimers())

  test('利用者操作からawaitを挟まずaudioなしで標準pickerを起動する', async () => {
    let resolveSelection: (stream: MediaStream) => void = () => undefined
    const selected = fakeStream()
    const harness = createHarness()
    harness.getDisplayMedia.mockImplementation(() => new Promise((resolve) => { resolveSelection = resolve }))

    const selecting = harness.controller.selectSurface('window')

    expect(harness.getDisplayMedia).toHaveBeenCalledTimes(1)
    expect(harness.getDisplayMedia).toHaveBeenCalledWith({
      video: { displaySurface: 'window' },
      audio: false,
      selfBrowserSurface: 'exclude',
      surfaceSwitching: 'exclude',
      monitorTypeSurfaces: 'exclude',
    })
    resolveSelection(selected)
    await selecting
  })

  test('monitorを検証してローカルpreviewだけをactiveにする', async () => {
    const track = new FakeTrack('monitor')
    const harness = createHarness({ stream: fakeStream(track) })

    await harness.controller.selectSurface('monitor')

    expect(harness.controller.snapshot()).toMatchObject({
      generation: 1,
      captureState: 'active',
      recognitionState: 'idle',
      actualSurface: 'monitor',
      targetLabel: '公開テスト画面',
    })
    expect(harness.preview.srcObject).toBeTruthy()
    expect(track.stop).not.toHaveBeenCalled()
    expect(harness.canvas.toBlob).not.toHaveBeenCalled()
  })

  test('window希望と実対象が一致した場合だけ共有する', async () => {
    const track = new FakeTrack('window')
    const harness = createHarness({ stream: fakeStream(track) })

    await harness.controller.selectSurface('window')

    expect(harness.controller.snapshot()).toMatchObject({
      captureState: 'active',
      requestedSurface: 'window',
      actualSurface: 'window',
    })
  })

  test('browser希望と実対象が一致した場合だけ共有する', async () => {
    const track = new FakeTrack('browser')
    const harness = createHarness({ stream: fakeStream(track) })

    await harness.controller.selectSurface('browser')

    expect(harness.getDisplayMedia).toHaveBeenCalledWith({
      video: { displaySurface: 'browser' },
      audio: false,
      selfBrowserSurface: 'exclude',
      surfaceSwitching: 'exclude',
      monitorTypeSurfaces: 'exclude',
    })
    expect(harness.controller.snapshot()).toMatchObject({
      captureState: 'active',
      requestedSurface: 'browser',
      actualSurface: 'browser',
    })
  })

  test.each([
    ['window', 'surface_mismatch'],
    [null, 'surface_unknown'],
  ] as const)('不正なsurface %sを停止して画像を作らない', async (surface, reason) => {
    const track = new FakeTrack(surface)
    const harness = createHarness({ stream: fakeStream(track) })

    await harness.controller.selectSurface('monitor')

    expect(track.stop).toHaveBeenCalledTimes(1)
    expect(harness.controller.snapshot()).toMatchObject({ captureState: 'unavailable', reasonCode: reason })
    expect(harness.canvas.toBlob).not.toHaveBeenCalled()
  })

  test.each([
    ['NotAllowedError', 'capture_not_allowed'],
    ['NotFoundError', 'no_capture_source'],
    ['NotReadableError', 'capture_os_error'],
    ['AbortError', 'capture_os_error'],
  ] as const)('pickerの%sを固定reason codeへ変換しraw messageを保持しない', async (name, reason) => {
    const harness = createHarness()
    harness.getDisplayMedia.mockRejectedValue(new DOMException('保存してはいけないraw message', name))

    await harness.controller.selectSurface('monitor')

    expect(harness.controller.snapshot()).toMatchObject({ captureState: 'unavailable', reasonCode: reason })
    expect(JSON.stringify(harness.states)).not.toContain('保存してはいけないraw message')
  })

  test('preview開始失敗時は現在streamを停止する', async () => {
    const track = new FakeTrack('monitor')
    const harness = createHarness({ stream: fakeStream(track) })
    vi.mocked(harness.preview.play).mockRejectedValue(new Error('raw playback failure'))

    await harness.controller.selectSurface('monitor')

    expect(track.stop).toHaveBeenCalledTimes(1)
    expect(harness.controller.snapshot()).toMatchObject({ captureState: 'unavailable', reasonCode: 'capture_os_error' })
  })

  test('対象変更では旧streamを止めてから新しいpickerを開始する', async () => {
    const monitorTrack = new FakeTrack('monitor')
    const windowTrack = new FakeTrack('window')
    const harness = createHarness({ stream: fakeStream(monitorTrack) })
    await harness.controller.selectSurface('monitor')
    harness.getDisplayMedia.mockImplementation(async () => {
      expect(monitorTrack.stop).toHaveBeenCalledTimes(1)
      return fakeStream(windowTrack)
    })

    await harness.controller.selectSurface('window')

    expect(harness.controller.snapshot()).toMatchObject({ generation: 2, captureState: 'active', actualSurface: 'window' })
  })

  test('audio trackまたは複数video相当を含むstreamを停止する', async () => {
    const video = new FakeTrack('monitor')
    const audio = new FakeTrack('monitor') as unknown as MediaStreamTrack
    const harness = createHarness({ stream: fakeStream(video, [audio]) })

    await harness.controller.selectSurface('monitor')

    expect(video.stop).toHaveBeenCalledTimes(1)
    expect((audio.stop as ReturnType<typeof vi.fn>)).toHaveBeenCalledTimes(1)
    expect(harness.controller.snapshot().reasonCode).toBe('video_track_invalid')
  })

  test.each([
    [false, true, 'insecure_context'],
    [true, false, 'transient_activation_required'],
  ] as const)('support条件を満たさない場合はpickerを呼ばない', async (secure, active, reason) => {
    const harness = createHarness({ secure, active })
    await harness.controller.selectSurface('monitor')
    expect(harness.getDisplayMedia).not.toHaveBeenCalled()
    expect(harness.controller.snapshot().reasonCode).toBe(reason)
  })

  test('APIがない場合はunsupportedとしてpickerを呼ばない', async () => {
    const harness = createHarness({ api: false })
    await harness.controller.selectSurface('monitor')
    expect(harness.getDisplayMedia).not.toHaveBeenCalled()
    expect(harness.controller.snapshot()).toMatchObject({ captureState: 'unsupported', reasonCode: 'api_unavailable' })
  })

  test('picker解決前のOFFで旧streamを即停止する', async () => {
    let resolveSelection: (stream: MediaStream) => void = () => undefined
    const track = new FakeTrack('monitor')
    const harness = createHarness()
    harness.getDisplayMedia.mockImplementation(() => new Promise((resolve) => { resolveSelection = resolve }))
    const selecting = harness.controller.selectSurface('monitor')

    harness.controller.stop('user_off')
    resolveSelection(fakeStream(track))
    await selecting

    expect(track.stop).toHaveBeenCalledTimes(1)
    expect(harness.controller.snapshot().captureState).toBe('off')
    expect(harness.preview.srcObject).toBeNull()
  })

  test('外部共有停止とmuteを現在generationだけへ反映する', async () => {
    const track = new FakeTrack('monitor')
    const harness = createHarness({ stream: fakeStream(track) })
    await harness.controller.selectSurface('monitor')

    track.muted = true
    track.dispatchEvent(new Event('mute'))
    expect(harness.controller.snapshot().trackMuted).toBe(true)
    track.dispatchEvent(new Event('ended'))

    expect(harness.controller.snapshot()).toMatchObject({ captureState: 'unavailable', reasonCode: 'frame_unavailable' })
    expect(harness.preview.srcObject).toBeNull()
  })

  test('Core認可requestごとに新しいframeを上限内へ縮小してmetadataを付ける', async () => {
    const harness = createHarness({ width: 4000, height: 3000 })
    await harness.controller.selectSurface('monitor')

    const captured = await harness.controller.captureSnapshot({
      request: snapshotRequest(1),
      clientSessionId: CLIENT_SESSION_ID,
    })

    expect(harness.drawImage).toHaveBeenCalledTimes(1)
    expect(captured.metadata).toMatchObject({
      screen_session_id: SCREEN_SESSION_ID,
      client_session_id: CLIENT_SESSION_ID,
      generation: 1,
      request_id: REQUEST_ID,
      turn_id: TURN_ID,
      actual_surface: 'monitor',
      captured_at: '2026-09-06T00:00:01.000Z',
      mime_type: 'image/png',
      byte_length: 128,
    })
    expect(captured.metadata.width).toBeLessThanOrEqual(SCREEN_CAPTURE_LIMITS.maxWidth)
    expect(captured.metadata.height).toBeLessThanOrEqual(SCREEN_CAPTURE_LIMITS.maxHeight)
    expect(captured.metadata.width * captured.metadata.height).toBeLessThanOrEqual(SCREEN_CAPTURE_LIMITS.maxPixels)
    expect(harness.controller.snapshot().recognitionState).toBe('uploading')
    expect(harness.controller.snapshot().lastRecognizedCaptureAt).toBeNull()
  })

  test('PNGがbyte上限を超えた場合だけJPEGへ変換する', async () => {
    const harness = createHarness({
      pngSize: SCREEN_CAPTURE_LIMITS.maxBytes + 1,
      jpegSize: 100,
    })
    await harness.controller.selectSurface('monitor')

    const captured = await harness.controller.captureSnapshot({
      request: snapshotRequest(1),
      clientSessionId: CLIENT_SESSION_ID,
    })

    expect(captured.metadata.mime_type).toBe('image/jpeg')
    expect(harness.canvas.toBlob).toHaveBeenNthCalledWith(1, expect.any(Function), 'image/png', undefined)
    expect(harness.canvas.toBlob).toHaveBeenNthCalledWith(2, expect.any(Function), 'image/jpeg', 0.92)
  })

  test('旧generation、期限切れ、frameなしではBlobを生成しない', async () => {
    const harness = createHarness()
    await harness.controller.selectSurface('monitor')

    await expect(harness.controller.captureSnapshot({
      request: snapshotRequest(2),
      clientSessionId: CLIENT_SESSION_ID,
    })).rejects.toSatisfy((error) => screenCaptureFailureReason(error) === 'generation_mismatch')
    expect(harness.canvas.toBlob).not.toHaveBeenCalled()

    harness.controller.stop('conversation_change')
    await expect(harness.controller.captureSnapshot({
      request: snapshotRequest(2),
      clientSessionId: CLIENT_SESSION_ID,
    })).rejects.toSatisfy((error) => screenCaptureFailureReason(error) === 'session_revoked')
    expect(harness.canvas.toBlob).not.toHaveBeenCalled()
  })

  test('frame寸法がない場合は画像を生成しない', async () => {
    const harness = createHarness({ width: 0, height: 0 })
    await harness.controller.selectSurface('monitor')

    await expect(harness.controller.captureSnapshot({
      request: snapshotRequest(1),
      clientSessionId: CLIENT_SESSION_ID,
    })).rejects.toSatisfy((error) => screenCaptureFailureReason(error) === 'frame_unavailable')
    expect(harness.canvas.toBlob).not.toHaveBeenCalled()
  })

  test('Edgeの静止window相当で次frame通知がなくても現在frameを取得する', async () => {
    vi.useFakeTimers()
    const harness = createHarness()
    Object.defineProperty(harness.preview, 'requestVideoFrameCallback', {
      configurable: true,
      value: vi.fn(() => 1),
    })
    Object.defineProperty(harness.preview, 'cancelVideoFrameCallback', {
      configurable: true,
      value: vi.fn(),
    })
    await harness.controller.selectSurface('monitor')
    const capturing = harness.controller.captureSnapshot({
      request: snapshotRequest(1),
      clientSessionId: CLIENT_SESSION_ID,
    })

    await vi.advanceTimersByTimeAsync(250)

    await expect(capturing).resolves.toMatchObject({
      metadata: { actual_surface: 'monitor' },
    })
    expect(harness.drawImage).toHaveBeenCalledTimes(1)
  })

  test('現在frameも次frame通知もなければ取得期限で失敗する', async () => {
    vi.useFakeTimers()
    const harness = createHarness({ readyState: HTMLMediaElement.HAVE_NOTHING })
    Object.defineProperty(harness.preview, 'requestVideoFrameCallback', {
      configurable: true,
      value: vi.fn(() => 1),
    })
    Object.defineProperty(harness.preview, 'cancelVideoFrameCallback', {
      configurable: true,
      value: vi.fn(),
    })
    await harness.controller.selectSurface('monitor')
    const capturing = harness.controller.captureSnapshot({
      request: snapshotRequest(1),
      clientSessionId: CLIENT_SESSION_ID,
    })
    const rejection = expect(capturing).rejects.toSatisfy(
      (error) => screenCaptureFailureReason(error) === 'request_expired',
    )

    await vi.advanceTimersByTimeAsync(4_000)

    await rejection
    expect(harness.drawImage).not.toHaveBeenCalled()
  })

  test('frame待機中のOFFで旧画像を生成しない', async () => {
    let frameCallback: (() => void) | undefined
    const harness = createHarness()
    Object.defineProperty(harness.preview, 'requestVideoFrameCallback', {
      configurable: true,
      value: vi.fn((callback: () => void) => { frameCallback = callback; return 1 }),
    })
    await harness.controller.selectSurface('monitor')
    const capturing = harness.controller.captureSnapshot({
      request: snapshotRequest(1),
      clientSessionId: CLIENT_SESSION_ID,
    })

    harness.controller.stop('pagehide')
    frameCallback?.()

    await expect(capturing).rejects.toSatisfy((error) => screenCaptureFailureReason(error) === 'generation_mismatch')
    expect(harness.canvas.toBlob).not.toHaveBeenCalled()
    expect(harness.controller.snapshot()).toMatchObject({ captureState: 'off', recognitionState: 'idle' })
  })

  test('新しい静止画要求は同じ共有内の古い要求をcancelして1枚だけ作る', async () => {
    const frameCallbacks: Array<() => void> = []
    const harness = createHarness()
    Object.defineProperty(harness.preview, 'requestVideoFrameCallback', {
      configurable: true,
      value: vi.fn((callback: () => void) => {
        frameCallbacks.push(callback)
        return frameCallbacks.length
      }),
    })
    Object.defineProperty(harness.preview, 'cancelVideoFrameCallback', {
      configurable: true,
      value: vi.fn(),
    })
    await harness.controller.selectSurface('monitor')
    const first = harness.controller.captureSnapshot({
      request: snapshotRequest(1),
      clientSessionId: CLIENT_SESSION_ID,
    })
    const firstRejection = expect(first).rejects.toSatisfy(
      (error) => screenCaptureFailureReason(error) === 'request_cancelled',
    )
    const second = harness.controller.captureSnapshot({
      request: { ...snapshotRequest(1), request_id: '80000000-0000-4000-8000-000000000001' },
      clientSessionId: CLIENT_SESSION_ID,
    })
    frameCallbacks[1]?.()

    await firstRejection
    const captured = await second
    expect(captured.metadata.request_id).toBe('80000000-0000-4000-8000-000000000001')
    expect(harness.canvas.toBlob).toHaveBeenCalledTimes(1)
    expect(harness.controller.snapshot()).toMatchObject({ recognitionState: 'uploading', reasonCode: null })
  })
})
