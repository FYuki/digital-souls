import { fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import ScreenCaptureControls from './ScreenCaptureControls.svelte'
import type { CapturedScreenSnapshot } from './screen-perception/capture'
import type { SnapshotRequested } from './screen-perception/generated'

const CONVERSATION_ID = '10000000-0000-4000-8000-000000000001'

class UiTrack extends EventTarget {
  label = '合成ウィンドウ'
  readyState: MediaStreamTrackState = 'live'
  muted = false
  stop = vi.fn(() => { this.readyState = 'ended' })
  constructor(private readonly surface: 'monitor' | 'window' = 'monitor') { super() }
  getSettings = () => ({ displaySurface: this.surface })
}

const createStream = (track: UiTrack) => ({
  getTracks: () => [track],
  getVideoTracks: () => [track],
  getAudioTracks: () => [],
}) as unknown as MediaStream

const request = (generation: number): SnapshotRequested => ({
  protocol_version: '1.0',
  type: 'screen_snapshot_requested',
  event_id: '20000000-0000-4000-8000-000000000001',
  screen_session_id: '30000000-0000-4000-8000-000000000001',
  generation,
  request_id: '40000000-0000-4000-8000-000000000001',
  turn_id: '50000000-0000-4000-8000-000000000001',
  source: 'explicit_ui',
  requested_at: '2099-01-01T00:00:00.000Z',
  capture_deadline: '2099-01-01T00:00:05.000Z',
})

describe('ScreenCaptureControls', () => {
  const getDisplayMedia = vi.fn()
  const play = vi.fn(async () => undefined)
  const pause = vi.fn()

  beforeEach(() => {
    getDisplayMedia.mockReset()
    play.mockClear()
    pause.mockClear()
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true })
    Object.defineProperty(navigator, 'userActivation', {
      configurable: true,
      value: { isActive: true },
    })
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getDisplayMedia },
    })
    Object.defineProperty(HTMLMediaElement.prototype, 'play', { configurable: true, value: play })
    Object.defineProperty(HTMLMediaElement.prototype, 'pause', { configurable: true, value: pause })
    Object.defineProperties(HTMLVideoElement.prototype, {
      videoWidth: { configurable: true, get: () => 1280 },
      videoHeight: { configurable: true, get: () => 720 },
      readyState: { configurable: true, get: () => HTMLMediaElement.HAVE_CURRENT_DATA },
      requestVideoFrameCallback: {
        configurable: true,
        value: (callback: () => void) => { callback(); return 1 },
      },
    })
  })

  test('初期OFFで明示参照は無効、通常操作に影響する永続化を行わない', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    render(ScreenCaptureControls, { props: { characterId: 'miori', conversationId: CONVERSATION_ID } })

    expect(screen.getByRole('button', { name: '画面共有を開始' }).getAttribute('aria-pressed')).toBe('false')
    expect((screen.getByRole('button', { name: '現在の画面を参照' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('共有: 停止中')).toBeTruthy()
    expect(screen.getByText(/音声だけで画面共有が自動開始することはありません/)).toBeTruthy()
    expect(screen.getByText(/通常の質問ごとに押す必要はありません/)).toBeTruthy()
    expect(setItem).not.toHaveBeenCalled()
  })

  test('monitor共有、preview、focus復帰、停止を行いONだけでは画像を作らない', async () => {
    const track = new UiTrack('monitor')
    getDisplayMedia.mockResolvedValue(createStream(track))
    const view = render(ScreenCaptureControls, { props: { characterId: 'miori', conversationId: CONVERSATION_ID } })
    const start = screen.getByRole('button', { name: '画面共有を開始' })

    await fireEvent.click(start)

    const stop = await screen.findByRole('button', { name: '画面共有を停止' })
    expect(stop.getAttribute('aria-pressed')).toBe('true')
    expect(document.activeElement).toBe(stop)
    expect(screen.getByText('共有: 共有中')).toBeTruthy()
    expect(screen.getByText('認識: 待機')).toBeTruthy()
    expect(screen.getByText(/共有ONだけでは画像の送信や定期的な解析を行いません/)).toBeTruthy()
    expect(screen.getByText(/特定のアプリについて尋ねる場合はウィンドウ共有が適しています/)).toBeTruthy()
    expect(screen.getByText(/digital-soulsを前面にすると/)).toBeTruthy()
    expect(screen.getByText(/対象（この画面だけに表示）: 合成ウィンドウ/)).toBeTruthy()
    expect(screen.getByLabelText('共有画面のローカルプレビュー').classList.contains('visible')).toBe(true)
    expect((screen.getByRole('button', { name: '現在の画面を参照' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/共有画像は送信されません/)).toBeTruthy()

    await view.rerender({ characterId: 'miori', conversationId: CONVERSATION_ID, disabled: true })
    expect((stop as HTMLButtonElement).disabled).toBe(false)
    await fireEvent.click(stop)
    expect(track.stop).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('共有: 停止中')).toBeTruthy()
  })

  test('選択中に停止すると遅れて返ったstreamを破棄する', async () => {
    let resolvePicker: (stream: MediaStream) => void = () => undefined
    const track = new UiTrack('monitor')
    getDisplayMedia.mockImplementation(() => new Promise((resolve) => { resolvePicker = resolve }))
    render(ScreenCaptureControls, { props: { characterId: 'miori', conversationId: CONVERSATION_ID } })

    await fireEvent.click(await screen.findByRole('button', { name: '画面共有を開始' }))
    await fireEvent.click(await screen.findByRole('button', { name: '画面共有を停止' }))
    resolvePicker(createStream(track))

    await waitFor(() => expect(track.stop).toHaveBeenCalledTimes(1))
    expect(screen.getByText('共有: 停止中')).toBeTruthy()
  })

  test('conversation変更、pagehide、Core通信断で現在streamを解放する', async () => {
    const tracks = [new UiTrack(), new UiTrack(), new UiTrack()]
    getDisplayMedia.mockImplementation(async () => createStream(tracks[getDisplayMedia.mock.calls.length - 1]!))
    const view = render(ScreenCaptureControls, {
      props: { characterId: 'miori', conversationId: CONVERSATION_ID, coreConnected: true },
    })

    await fireEvent.click(await screen.findByRole('button', { name: '画面共有を開始' }))
    await view.rerender({ characterId: 'miori', conversationId: '60000000-0000-4000-8000-000000000001', coreConnected: true })
    await waitFor(() => expect(tracks[0]?.stop).toHaveBeenCalledTimes(1))

    await fireEvent.click(await screen.findByRole('button', { name: '画面共有を開始' }))
    window.dispatchEvent(new PageTransitionEvent('pagehide'))
    await waitFor(() => expect(tracks[1]?.stop).toHaveBeenCalledTimes(1))

    await fireEvent.click(await screen.findByRole('button', { name: '画面共有を開始' }))
    await view.rerender({ characterId: 'miori', conversationId: '60000000-0000-4000-8000-000000000001', coreConnected: false })
    await waitFor(() => expect(tracks[2]?.stop).toHaveBeenCalledTimes(1))
  })

  test('Core認可がある明示要求だけを1枚ずつcallbackへ渡す', async () => {
    const track = new UiTrack('monitor')
    getDisplayMedia.mockResolvedValue(createStream(track))
    const requestSnapshot = vi.fn(async () => ({
      request: request(1),
      clientSessionId: '70000000-0000-4000-8000-000000000001',
    }))
    const onSnapshotCaptured = vi.fn<(snapshot: CapturedScreenSnapshot) => Promise<void>>(async () => undefined)
    const context = { drawImage: vi.fn() }
    Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
      configurable: true,
      value: vi.fn(() => context),
    })
    Object.defineProperty(HTMLCanvasElement.prototype, 'toBlob', {
      configurable: true,
      value: vi.fn((callback: BlobCallback, type?: string) => {
        callback(new Blob([new Uint8Array(32)], { type: type ?? 'image/png' }))
      }),
    })
    render(ScreenCaptureControls, {
      props: {
        characterId: 'miori',
        conversationId: CONVERSATION_ID,
        coreConnected: true,
        requestSnapshot,
        onSnapshotCaptured,
      },
    })
    await fireEvent.click(screen.getByRole('button', { name: '画面共有を開始' }))

    expect(requestSnapshot).not.toHaveBeenCalled()
    expect(onSnapshotCaptured).not.toHaveBeenCalled()
    await fireEvent.click(await screen.findByRole('button', { name: '現在の画面を参照' }))

    await waitFor(() => expect(onSnapshotCaptured).toHaveBeenCalledTimes(1))
    expect(requestSnapshot).toHaveBeenCalledTimes(1)
    expect(context.drawImage).toHaveBeenCalledTimes(1)
    expect(onSnapshotCaptured.mock.calls[0]?.[0].metadata).toMatchObject({
      generation: 1,
      request_id: '40000000-0000-4000-8000-000000000001',
      actual_surface: 'monitor',
    })
  })

  test('非対応環境でも状態を通知し、画面参照以外は封鎖しない', async () => {
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: false })
    render(ScreenCaptureControls, { props: { characterId: 'miori', conversationId: CONVERSATION_ID } })

    await fireEvent.click(screen.getByRole('button', { name: '画面共有を開始' }))

    expect(screen.getByText('共有: 非対応')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('安全な接続ではないため')
    expect(getDisplayMedia).not.toHaveBeenCalled()
  })

  test('Core認可取得の失敗はraw errorを出さず画像を生成しない', async () => {
    const track = new UiTrack('monitor')
    getDisplayMedia.mockResolvedValue(createStream(track))
    const requestSnapshot = vi.fn(async () => {
      throw new Error('保存してはいけないBackend raw error')
    })
    const onSnapshotCaptured = vi.fn<(snapshot: CapturedScreenSnapshot) => Promise<void>>(async () => undefined)
    render(ScreenCaptureControls, {
      props: {
        characterId: 'miori',
        conversationId: CONVERSATION_ID,
        coreConnected: true,
        requestSnapshot,
        onSnapshotCaptured,
      },
    })
    await fireEvent.click(screen.getByRole('button', { name: '画面共有を開始' }))
    await fireEvent.click(await screen.findByRole('button', { name: '現在の画面を参照' }))

    expect((await screen.findByRole('alert')).textContent).toContain('サーバー連携は準備中です')
    expect(document.body.textContent).not.toContain('保存してはいけないBackend raw error')
    expect(onSnapshotCaptured).not.toHaveBeenCalled()
  })

  test('Core認可待機中は参照要求をsingle-flightにする', async () => {
    const track = new UiTrack('monitor')
    getDisplayMedia.mockResolvedValue(createStream(track))
    let resolveAuthorization: (value: null) => void = () => undefined
    const requestSnapshot = vi.fn(() => new Promise<null>((resolve) => { resolveAuthorization = resolve }))
    const onSnapshotCaptured = vi.fn<(snapshot: CapturedScreenSnapshot) => Promise<void>>(async () => undefined)
    render(ScreenCaptureControls, {
      props: {
        characterId: 'miori',
        conversationId: CONVERSATION_ID,
        coreConnected: true,
        requestSnapshot,
        onSnapshotCaptured,
      },
    })
    await fireEvent.click(screen.getByRole('button', { name: '画面共有を開始' }))
    const reference = await screen.findByRole('button', { name: '現在の画面を参照' })
    await fireEvent.click(reference)

    expect((reference as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('認識: 参照要求を確認中')).toBeTruthy()
    await fireEvent.click(reference)
    expect(requestSnapshot).toHaveBeenCalledTimes(1)

    resolveAuthorization(null)
    expect((await screen.findByRole('alert')).textContent).toContain('サーバー連携は準備中です')
    expect(onSnapshotCaptured).not.toHaveBeenCalled()
  })

  test('会話runtimeの参照判断中を取得や常時解析と区別して表示する', async () => {
    const track = new UiTrack('monitor')
    getDisplayMedia.mockResolvedValue(createStream(track))
    const view = render(ScreenCaptureControls, {
      props: {
        characterId: 'miori',
        conversationId: CONVERSATION_ID,
        contextualReferenceAvailable: true,
        referenceDecisionActive: false,
      },
    })
    await fireEvent.click(screen.getByRole('button', { name: '画面共有を開始' }))
    expect(screen.getByText('認識: 参照可能')).toBeTruthy()

    await view.rerender({
      characterId: 'miori',
      conversationId: CONVERSATION_ID,
      contextualReferenceAvailable: true,
      referenceDecisionActive: true,
    })

    expect(screen.getByText('認識: 参照が必要か確認中')).toBeTruthy()
    expect(getDisplayMedia).toHaveBeenCalledTimes(1)
    expect((screen.getByRole('button', { name: '現在の画面を参照' }) as HTMLButtonElement).disabled).toBe(true)
  })

  test('window共有では取得対象とdigital-soulsを区別して案内する', async () => {
    const track = new UiTrack('window')
    getDisplayMedia.mockResolvedValue(createStream(track))
    render(ScreenCaptureControls, {
      props: { characterId: 'miori', conversationId: CONVERSATION_ID },
    })
    await fireEvent.change(screen.getByLabelText('共有する画面の種類'), { target: { value: 'window' } })
    await fireEvent.click(screen.getByRole('button', { name: '画面共有を開始' }))

    await screen.findByText('共有: 共有中')
    expect(screen.getByText(/選択したウィンドウが参照対象です/)).toBeTruthy()
    expect(screen.getByText(/digital-soulsの画面とは別の対象/)).toBeTruthy()
  })

  test('クラウド画像と派生テキストの同意を分けてruntimeへ通知する', async () => {
    const onCloudImageConsentChanged = vi.fn()
    const onCloudDerivedChatConsentChanged = vi.fn()
    render(ScreenCaptureControls, {
      props: {
        characterId: 'miori',
        conversationId: CONVERSATION_ID,
        cloudImageConsent: false,
        cloudDerivedChatConsent: false,
        onCloudImageConsentChanged,
        onCloudDerivedChatConsentChanged,
      },
    })

    const imageConsent = screen.getByRole('checkbox', { name: /今回の共有画像をクラウドの画面認識へ送信する/ })
    const derivedConsent = screen.getByRole('checkbox', { name: /画面の観測文と、この共有に由来する会話内の回答/ })
    expect(screen.getByText(/現在の共有対象・会話・送信先だけに有効/)).toBeTruthy()

    await fireEvent.click(imageConsent)
    await fireEvent.click(derivedConsent)

    expect(onCloudImageConsentChanged).toHaveBeenCalledWith(true)
    expect(onCloudDerivedChatConsentChanged).toHaveBeenCalledWith(true)
  })
})
