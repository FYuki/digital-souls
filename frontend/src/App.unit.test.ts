import { act, fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import App from './App.svelte'

const CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const SECOND_CONVERSATION_ID = '6ad9a610-02cc-4a41-b02e-503826f7292b'
const THIRD_CONVERSATION_ID = 'f98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const TURN_ID = '9e70795d-e5d5-431d-baa2-67f884403010'
const VOICE_SESSION_ID = '20000000-0000-4000-8000-000000000010'
const VOICE_PARTICIPANT_ID = '40000000-0000-4000-8000-000000000010'
const RESPONSE_ID = '50000000-0000-4000-8000-000000000010'

const conversation = {
  character_id: 'miori',
  conversation_id: CONVERSATION_ID,
  created_at: '2026-08-01T12:00:00+00:00',
  updated_at: '2026-08-01T12:01:00+00:00',
  archived_at: null,
  title: CONVERSATION_ID,
}

const audioMocks = vi.hoisted(() => ({
  pcmData: new ArrayBuffer(4),
  vadStart: vi.fn(),
  vadDestroy: vi.fn(),
  recorderInitialize: vi.fn(),
  recorderStart: vi.fn(),
  recorderStopAndTake: vi.fn(),
  recorderClose: vi.fn(),
  getUserMedia: vi.fn(),
  microphoneStream: { getTracks: () => [] } as unknown as MediaStream,
  vadOptions: undefined as
    | {
        onSpeechStart: () => void
        onSpeechRealStart: () => void
        onSpeechEnd: () => void
      }
    | undefined,
}))

vi.mock('@ricky0123/vad-web', () => ({
  MicVAD: {
    new: vi.fn(async (options) => {
      audioMocks.vadOptions = options
      return {
        start: () => audioMocks.vadStart(),
        destroy: () => audioMocks.vadDestroy(),
      }
    }),
  },
}))

vi.mock('./lib/audio/pcm-worklet-recorder', () => ({
  AudioWorkletPcmRecorder: vi.fn(() => ({
    initialize: audioMocks.recorderInitialize,
    start: audioMocks.recorderStart,
    stopAndTake: audioMocks.recorderStopAndTake,
    close: audioMocks.recorderClose,
  })),
}))

const decodeAudioData = vi.fn()
const createBufferSource = vi.fn()
const connect = vi.fn()
const start = vi.fn()
const close = vi.fn()
const fetchMock = vi.fn()

type CoreEventReceiver = (event: Record<string, unknown>) => void
type RoomObserver = (event: Record<string, unknown>) => void
const liveKitMocks = {
  connect: vi.fn(async () => undefined),
  publishMicrophone: vi.fn(async () => undefined),
  muteMicrophone: vi.fn(async () => undefined),
  stopPlayback: vi.fn(() => 0),
  publishControlEvent: vi.fn(async (event: Record<string, unknown>) => {
    liveKitMocks.controlEvents.push(event)
  }),
  disconnect: vi.fn(),
  controlEvents: [] as Record<string, unknown>[],
  receiveCoreEvent: undefined as CoreEventReceiver | undefined,
  observeRoom: undefined as RoomObserver | undefined,
}

class FakeAudioContext {
  destination = {}
  decodeAudioData = decodeAudioData
  createBufferSource = createBufferSource
  close = close
}

let turnSequence = 0

const persistedTurn = (userContent: string, assistantContent: string) => ({
  kind: 'content',
  turn_id: `9e70795d-e5d5-431d-baa2-${String(++turnSequence).padStart(12, '0')}`,
  user_content: userContent,
  assistant_content: assistantContent,
})

const defaultFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = String(input)
  if (url === '/api/voice/livekit/token') {
    return new Response(JSON.stringify({
      session_id: VOICE_SESSION_ID,
      participant_id: VOICE_PARTICIPANT_ID,
      room: 'mock-room',
      token: 'mock-token',
      livekit_url: 'ws://mock-livekit.invalid',
      expires_at: '2026-08-28T12:00:00.000Z',
      reconnect_grace_ms: 60_000,
    }), { status: 200 })
  }
  if (url.startsWith('/api/voice/livekit/sessions/') && init?.method === 'DELETE') {
    return new Response(null, { status: 204 })
  }
  if (url === '/api/chat') {
    const body = JSON.parse(String(init?.body)) as Record<string, string>
    return new Response(JSON.stringify({
      character: body.character,
      turn: persistedTurn(body.message, 'HTTP応答です。'),
    }), { status: 200 })
  }
  if (url.endsWith('/turns')) {
    return new Response(JSON.stringify([
      persistedTurn('保存済みの質問', '保存済みの回答'),
    ]), { status: 200 })
  }
  if (url.endsWith('/archived')) return new Response('[]', { status: 200 })
  if (init?.method === 'POST') return new Response(JSON.stringify(conversation), { status: 200 })
  return new Response(JSON.stringify([conversation]), { status: 200 })
}

const selectConversation = async (): Promise<void> => {
  await fireEvent.click(
    await screen.findByRole('button', { name: new RegExp(`^${CONVERSATION_ID}$`) }),
  )
}

const startLiveKitSession = async () => {
  await selectConversation()
  await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
  await waitFor(() => expect(liveKitMocks.publishMicrophone).toHaveBeenCalledTimes(1))
}

const emitCoreEvent = async (event: Record<string, unknown>) => {
  const receiver = liveKitMocks.receiveCoreEvent
  if (receiver === undefined) throw new Error('LiveKit core event receiver is required')
  await act(() => receiver({ session_id: VOICE_SESSION_ID, ...event }))
}

describe('App conversation lifecycle', () => {
  beforeEach(() => {
    localStorage.clear()
    window.history.replaceState({}, '', '/')
    turnSequence = 0
    audioMocks.vadOptions = undefined
    audioMocks.vadStart.mockReset().mockResolvedValue(undefined)
    audioMocks.vadDestroy.mockReset().mockResolvedValue(undefined)
    audioMocks.recorderInitialize.mockReset().mockResolvedValue(undefined)
    audioMocks.recorderStart.mockReset()
    audioMocks.recorderStopAndTake.mockReset().mockResolvedValue(audioMocks.pcmData)
    audioMocks.recorderClose.mockReset().mockResolvedValue(undefined)
    audioMocks.getUserMedia.mockReset().mockResolvedValue(audioMocks.microphoneStream)
    decodeAudioData.mockReset().mockResolvedValue({ duration: 1 })
    createBufferSource.mockReset().mockReturnValue({ connect, start })
    connect.mockReset()
    start.mockReset()
    close.mockReset().mockResolvedValue(undefined)
    liveKitMocks.connect.mockReset().mockResolvedValue(undefined)
    liveKitMocks.publishMicrophone.mockReset().mockResolvedValue(undefined)
    liveKitMocks.muteMicrophone.mockReset().mockResolvedValue(undefined)
    liveKitMocks.stopPlayback.mockReset().mockReturnValue(0)
    liveKitMocks.publishControlEvent.mockReset().mockImplementation(
      async (event: Record<string, unknown>) => {
        liveKitMocks.controlEvents.push(event)
      },
    )
    liveKitMocks.disconnect.mockReset()
    liveKitMocks.controlEvents = []
    liveKitMocks.receiveCoreEvent = undefined
    liveKitMocks.observeRoom = undefined
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => TURN_ID) })
    fetchMock.mockReset().mockImplementation(async (input, init) => defaultFetch(input, init))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('AudioContext', FakeAudioContext)
    vi.stubGlobal('navigator', {
      mediaDevices: { getUserMedia: audioMocks.getUserMedia },
    })
    vi.stubGlobal('__digitalSoulsVoiceSessionTestPort', {
      createRoom: (observe: RoomObserver, receiveCoreEvent: CoreEventReceiver) => {
        liveKitMocks.observeRoom = observe
        liveKitMocks.receiveCoreEvent = receiveCoreEvent
        return {
          connect: liveKitMocks.connect,
          publishMicrophone: liveKitMocks.publishMicrophone,
          muteMicrophone: liveKitMocks.muteMicrophone,
          stopPlayback: liveKitMocks.stopPlayback,
          publishControlEvent: liveKitMocks.publishControlEvent,
          disconnect: liveKitMocks.disconnect,
        }
      },
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  test('activeスレッドを読み込み、選択時に保存済み履歴だけを表示する', async () => {
    render(App)
    const thread = await screen.findByRole('button', { name: new RegExp(`^${CONVERSATION_ID}$`) })

    await fireEvent.click(thread)

    expect(await screen.findByText('保存済みの質問')).toBeTruthy()
    expect(screen.getByText('保存済みの回答')).toBeTruthy()
    expect(localStorage.getItem('digital-souls:conversation:miori')).toBe(CONVERSATION_ID)
  })

  test('通常UIからLiveKit sessionを開始し継続VADと順序付きdeltaを表示する', async () => {
    render(App)
    await startLiveKitSession()
    if (audioMocks.vadOptions === undefined) throw new Error('VAD callbacks are required')

    audioMocks.vadOptions.onSpeechStart()
    audioMocks.vadOptions.onSpeechRealStart()
    audioMocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(
      liveKitMocks.controlEvents.map((event) => event.type),
    ).toEqual([
      'session_start_requested',
      'session_resumed',
      'speech_started',
      'speech_stopped',
      'observation',
    ]))
    const tokenCall = fetchMock.mock.calls.find(([url]) => String(url) === '/api/voice/livekit/token')
    expect(JSON.parse(String(tokenCall?.[1]?.body))).toMatchObject({
      character_id: 'miori',
      conversation_id: CONVERSATION_ID,
    })

    await emitCoreEvent({
      type: 'utterance_finalized',
      utterance_id: TURN_ID,
      transcript: '継続入力の質問',
      should_response: true,
    })
    await emitCoreEvent({
      type: 'response_started',
      response_id: RESPONSE_ID,
      source_utterance_ids: [TURN_ID],
    })
    await emitCoreEvent({
      type: 'response_delta', response_id: RESPONSE_ID,
      text_sequence: 1, text: '逐次',
    })
    await emitCoreEvent({
      type: 'response_delta', response_id: RESPONSE_ID,
      text_sequence: 1, text: '重複',
    })
    await emitCoreEvent({
      type: 'response_delta', response_id: RESPONSE_ID,
      text_sequence: 3, text: '順序外',
    })
    await emitCoreEvent({
      type: 'response_delta', response_id: RESPONSE_ID,
      text_sequence: 2, text: '応答',
    })

    expect(screen.getByText('継続入力の質問')).toBeTruthy()
    expect(screen.getByText('逐次応答')).toBeTruthy()
    expect(screen.queryByText(/重複|順序外/)).toBeNull()
    expect(screen.getByRole('button', { name: 'マイクをオフにする' })).toBeTruthy()
    expect(audioMocks.vadStart).toHaveBeenCalledTimes(1)
    expect(audioMocks.vadDestroy).not.toHaveBeenCalled()
  })

  test('cancel後の遅延deltaを破棄し、同じsessionで次の発話を表示する', async () => {
    const nextUtteranceId = '30000000-0000-4000-8000-000000000020'
    const nextResponseId = '50000000-0000-4000-8000-000000000020'
    render(App)
    await startLiveKitSession()

    await emitCoreEvent({
      type: 'utterance_finalized', utterance_id: TURN_ID,
      transcript: '中断される質問', should_response: true,
    })
    await emitCoreEvent({
      type: 'response_started', response_id: RESPONSE_ID,
      source_utterance_ids: [TURN_ID],
    })
    await emitCoreEvent({
      type: 'response_delta', response_id: RESPONSE_ID,
      text_sequence: 1, text: '古い途中応答',
    })
    await emitCoreEvent({ type: 'response_cancelled', response_id: RESPONSE_ID })
    await emitCoreEvent({
      type: 'response_delta', response_id: RESPONSE_ID,
      text_sequence: 2, text: '混入してはいけない',
    })

    await emitCoreEvent({
      type: 'utterance_finalized', utterance_id: nextUtteranceId,
      transcript: '次の質問', should_response: true,
    })
    await emitCoreEvent({
      type: 'response_started', response_id: nextResponseId,
      source_utterance_ids: [nextUtteranceId],
    })
    await emitCoreEvent({
      type: 'response_delta', response_id: nextResponseId,
      text_sequence: 1, text: '次の応答',
    })

    expect(screen.queryByText('古い途中応答')).toBeNull()
    expect(screen.queryByText('混入してはいけない')).toBeNull()
    expect(screen.getByText('次の質問')).toBeTruthy()
    expect(screen.getByText('次の応答')).toBeTruthy()
    expect(liveKitMocks.publishMicrophone).toHaveBeenCalledTimes(1)
    expect(audioMocks.vadDestroy).not.toHaveBeenCalled()
    await waitFor(() => expect(
      fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/turns')).length,
    ).toBeGreaterThanOrEqual(2))
  })

  test('response失敗後もユーザー発話と途中回答を画面に保持する', async () => {
    render(App)
    await startLiveKitSession()

    await emitCoreEvent({
      type: 'utterance_finalized', utterance_id: TURN_ID,
      transcript: '消してはいけない質問', should_response: true,
    })
    await emitCoreEvent({
      type: 'response_started', response_id: RESPONSE_ID,
      source_utterance_ids: [TURN_ID],
    })
    await emitCoreEvent({
      type: 'response_delta', response_id: RESPONSE_ID,
      text_sequence: 1, text: '途中までの回答',
    })
    await emitCoreEvent({
      type: 'response_failed', response_id: RESPONSE_ID,
      reason: 'streaming_pipeline_failed',
    })

    expect(screen.getByText('消してはいけない質問')).toBeTruthy()
    expect(screen.getByText('途中までの回答')).toBeTruthy()
    expect(screen.getByText('光織（応答失敗）')).toBeTruthy()
    expect(screen.queryByText('光織（応答中）')).toBeNull()
  })

  test('入力・応答・再生・sessionを独立表示しbarge-inを制御する', async () => {
    render(App)
    await startLiveKitSession()
    await emitCoreEvent({
      type: 'response_started', response_id: RESPONSE_ID,
      source_utterance_ids: [TURN_ID],
    })
    await act(() => liveKitMocks.observeRoom?.({
      transport: 'available', control: 'available', audio: 'available',
      activeResponseId: RESPONSE_ID, renderedEnergy: 1,
    }))

    expect(screen.getByText('セッション: 接続済み')).toBeTruthy()
    expect(screen.getByText('入力: 聞き取り中')).toBeTruthy()
    expect(screen.getByText('応答: 応答生成中')).toBeTruthy()
    expect(screen.getByText('再生: 再生中')).toBeTruthy()
    if (audioMocks.vadOptions === undefined) throw new Error('VAD callbacks are required')

    audioMocks.vadOptions.onSpeechStart()
    audioMocks.vadOptions.onSpeechRealStart()
    await waitFor(() => expect(
      liveKitMocks.controlEvents.map((event) => event.type),
    ).toContain('speech_started'))
    expect(liveKitMocks.stopPlayback).not.toHaveBeenCalled()

    await emitCoreEvent({
      type: 'turn_decision',
      utterance_id: TURN_ID,
      response_id: RESPONSE_ID,
      decision: 'take_turn',
      final: false,
    })
    await waitFor(() => expect(liveKitMocks.stopPlayback).toHaveBeenCalledWith(
      RESPONSE_ID, expect.any(Number),
    ))
    expect(liveKitMocks.controlEvents.map((event) => event.type))
      .not.toContain('response_cancel_requested')

    expect(screen.getByText('応答: 割り込み処理中')).toBeTruthy()
    expect(screen.getByText('再生: 停止済み')).toBeTruthy()
  })

  test('音声session中のtext送信は音声を終了してからHTTP経路を使う', async () => {
    render(App)
    await startLiveKitSession()
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: 'テキストへ切り替える' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    await screen.findByText('HTTP応答です。')
    expect(liveKitMocks.disconnect).toHaveBeenCalledTimes(1)
    const requests = fetchMock.mock.calls.map(([input, init]) => ({
      url: String(input), method: init?.method,
    }))
    const endIndex = requests.findIndex((request) => (
      request.url.includes('/api/voice/livekit/sessions/')
      && request.method === 'DELETE'
    ))
    const chatIndex = requests.findIndex((request) => request.url === '/api/chat')
    expect(endIndex).toBeGreaterThanOrEqual(0)
    expect(chatIndex).toBeGreaterThan(endIndex)
  })

  test('音声session終了APIが失敗してもtext送信を継続する', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).startsWith('/api/voice/livekit/sessions/') && init?.method === 'DELETE') {
        return new Response(null, { status: 503 })
      }
      return defaultFetch(input, init)
    })
    render(App)
    await startLiveKitSession()
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '終了失敗後も送信する' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(await screen.findByText('HTTP応答です。')).toBeTruthy()
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/chat')).toBe(true)
  })

  test('recoverable音声エラー後もmicを維持して次の応答を処理する', async () => {
    const nextUtteranceId = '30000000-0000-4000-8000-000000000030'
    const nextResponseId = '50000000-0000-4000-8000-000000000030'
    render(App)
    await startLiveKitSession()

    await emitCoreEvent({
      type: 'error', utterance_id: TURN_ID,
      error_code: 'stt_inference_timeout', recoverable: true,
    })
    expect((await screen.findByRole('alert')).textContent).toBe('応答の取得に失敗しました。')
    expect(screen.getByRole('button', { name: 'マイクをオフにする' })).toBeTruthy()

    await emitCoreEvent({
      type: 'utterance_finalized', utterance_id: nextUtteranceId,
      transcript: '復旧後の質問', should_response: true,
    })
    await emitCoreEvent({
      type: 'response_started', response_id: nextResponseId,
      source_utterance_ids: [nextUtteranceId],
    })
    await emitCoreEvent({
      type: 'response_delta', response_id: nextResponseId,
      text_sequence: 1, text: '復旧後の応答',
    })

    expect(screen.getByText('復旧後の質問')).toBeTruthy()
    expect(screen.getByText('復旧後の応答')).toBeTruthy()
    expect(liveKitMocks.publishMicrophone).toHaveBeenCalledTimes(1)
  })


  test('履歴取得中に完了したHTTP turnを履歴応答後も維持する', async () => {
    const historicalTurn = persistedTurn('過去の質問', '過去の回答')
    let resolveHistory: ((response: Response) => void) | undefined
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/turns')) {
        return new Promise<Response>((resolve) => { resolveHistory = resolve })
      }
      return defaultFetch(input, init)
    })
    render(App)
    await selectConversation()
    await waitFor(() => expect(resolveHistory).toBeDefined())

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '取得中のテキスト質問' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))
    expect(await screen.findByText('HTTP応答です。')).toBeTruthy()
    if (resolveHistory === undefined) throw new Error('History resolver is required')
    const resolveTurns = resolveHistory
    await act(() => resolveTurns(new Response(
      JSON.stringify([historicalTurn]),
      { status: 200 },
    )))

    expect(await screen.findByText('過去の回答')).toBeTruthy()
    expect(screen.getByText('取得中のテキスト質問')).toBeTruthy()
    expect(screen.getByText('HTTP応答です。')).toBeTruthy()
  })

  test('新規作成後に初期active一覧の古い応答が到着しても作成したスレッドを維持する', async () => {
    let resolveInitialList: ((response: Response) => void) | undefined
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/api/characters/miori/conversations' && init === undefined) {
        return new Promise<Response>((resolve) => { resolveInitialList = resolve })
      }
      return defaultFetch(input, init)
    })
    render(App)
    await waitFor(() => expect(resolveInitialList).toBeDefined())

    await fireEvent.click(screen.getByRole('button', { name: '新規スレッド' }))
    await screen.findByRole('button', { name: CONVERSATION_ID })
    await waitFor(() => expect(
      screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled,
    ).toBe(false))
    if (resolveInitialList === undefined) throw new Error('Initial list resolver is required')
    const resolveList = resolveInitialList
    await act(() => resolveList(new Response('[]', { status: 200 })))

    expect(screen.getByRole('button', { name: CONVERSATION_ID })).toBeTruthy()
    expect(localStorage.getItem('digital-souls:conversation:miori')).toBe(CONVERSATION_ID)
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(false)
  })

  test('復元後に初期active一覧の古い応答が到着しても復元したスレッドを維持する', async () => {
    const archivedConversation = {
      ...conversation,
      archived_at: '2026-08-01T12:02:00+00:00',
    }
    let activeListRequestCount = 0
    let archivedListRequestCount = 0
    let resolveInitialList: ((response: Response) => void) | undefined
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/api/characters/miori/conversations' && init === undefined) {
        activeListRequestCount += 1
        if (activeListRequestCount === 1) {
          return new Promise<Response>((resolve) => { resolveInitialList = resolve })
        }
        return new Response(JSON.stringify([conversation]), { status: 200 })
      }
      if (url.endsWith('/archived')) {
        archivedListRequestCount += 1
        const archived = archivedListRequestCount === 1 ? [archivedConversation] : []
        return new Response(JSON.stringify(archived), { status: 200 })
      }
      return defaultFetch(input, init)
    })
    render(App)
    await waitFor(() => expect(resolveInitialList).toBeDefined())

    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))
    await fireEvent.click(await screen.findByRole('button', { name: `復元 ${CONVERSATION_ID}` }))
    expect(activeListRequestCount).toBe(1)
    await fireEvent.click(screen.getByRole('button', { name: 'アクティブ' }))
    await screen.findByRole('button', { name: CONVERSATION_ID })
    if (resolveInitialList === undefined) throw new Error('Initial list resolver is required')
    const resolveList = resolveInitialList
    await act(() => resolveList(new Response('[]', { status: 200 })))

    expect(screen.getByRole('button', { name: CONVERSATION_ID })).toBeTruthy()
  })

  test('archive成功後は一覧を再取得せず応答から即時に状態を遷移する', async () => {
    const archivedConversation = {
      ...conversation,
      archived_at: '2026-08-01T13:00:00+00:00',
    }
    let activeListRequestCount = 0
    let archivedListRequestCount = 0
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (init?.method === 'POST' && url.endsWith('/archive')) {
        return new Response(JSON.stringify(archivedConversation), { status: 200 })
      }
      if (url.endsWith('/archived')) {
        archivedListRequestCount += 1
        return new Promise<Response>(() => {})
      }
      if (url.endsWith('/conversations')) {
        activeListRequestCount += 1
        return new Response(JSON.stringify([conversation]), { status: 200 })
      }
      return defaultFetch(input, init)
    })
    render(App)
    await selectConversation()
    await screen.findByText('保存済みの回答')

    await fireEvent.click(screen.getByRole('button', { name: `アーカイブ ${CONVERSATION_ID}` }))

    await waitFor(() => expect(
      screen.queryByRole('button', { name: CONVERSATION_ID }),
    ).toBeNull())
    expect(screen.queryByText('保存済みの回答')).toBeNull()
    expect(localStorage.getItem('digital-souls:conversation:miori')).toBeNull()
    expect(activeListRequestCount).toBe(1)
    expect(archivedListRequestCount).toBe(0)
    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))
    expect(await screen.findByRole('button', { name: CONVERSATION_ID })).toBeTruthy()
    expect(archivedListRequestCount).toBe(1)
  })

  test('unarchive成功後は一覧を再取得せずactiveの更新日時降順を維持する', async () => {
    const archivedConversation = {
      ...conversation,
      archived_at: '2026-08-01T13:00:00+00:00',
    }
    const newerConversation = {
      ...conversation,
      conversation_id: SECOND_CONVERSATION_ID,
      updated_at: '2026-08-01T12:02:00+00:00',
    }
    const sameTimeConversation = {
      ...conversation,
      conversation_id: THIRD_CONVERSATION_ID,
    }
    let activeListRequestCount = 0
    let archivedListRequestCount = 0
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (init?.method === 'POST' && url.endsWith('/unarchive')) {
        return new Response(JSON.stringify(conversation), { status: 200 })
      }
      if (url.endsWith('/archived')) {
        archivedListRequestCount += 1
        return new Response(JSON.stringify([archivedConversation]), { status: 200 })
      }
      if (url.endsWith('/conversations')) {
        activeListRequestCount += 1
        return new Response(JSON.stringify([newerConversation, sameTimeConversation]), { status: 200 })
      }
      return defaultFetch(input, init)
    })
    render(App)
    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))
    await fireEvent.click(await screen.findByRole('button', { name: `復元 ${CONVERSATION_ID}` }))

    await fireEvent.click(screen.getByRole('button', { name: 'アクティブ' }))

    expect(await screen.findByRole('button', { name: CONVERSATION_ID })).toBeTruthy()
    const threadIds = screen.getAllByRole('button', { name: /^[0-9a-f-]{36}$/ })
      .map((button) => button.textContent)
    expect(threadIds).toEqual([
      SECOND_CONVERSATION_ID,
      THIRD_CONVERSATION_ID,
      CONVERSATION_ID,
    ])
    expect(activeListRequestCount).toBe(1)
    expect(archivedListRequestCount).toBe(1)
  })

  test('hard delete成功後は一覧を再取得せず対象状態を即時に除去する', async () => {
    const archivedConversation = {
      ...conversation,
      archived_at: '2026-08-01T13:00:00+00:00',
    }
    let activeListRequestCount = 0
    let archivedListRequestCount = 0
    localStorage.setItem('digital-souls:conversation:miori', CONVERSATION_ID)
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (init?.method === 'DELETE') {
        return new Response(null, { status: 204 })
      }
      if (url.endsWith('/archived')) {
        archivedListRequestCount += 1
        return new Response(JSON.stringify([archivedConversation]), { status: 200 })
      }
      if (url.endsWith('/conversations')) {
        activeListRequestCount += 1
        return new Response('[]', { status: 200 })
      }
      return defaultFetch(input, init)
    })
    render(App)
    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))
    await fireEvent.click(await screen.findByRole('button', { name: `削除 ${CONVERSATION_ID}` }))

    await fireEvent.click(screen.getByRole('button', { name: '完全に削除' }))

    await waitFor(() => expect(
      screen.queryByRole('button', { name: CONVERSATION_ID }),
    ).toBeNull())
    expect(localStorage.getItem('digital-souls:conversation:miori')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(activeListRequestCount).toBe(1)
    expect(archivedListRequestCount).toBe(1)
  })

  test('hard delete後に到着した古いarchived一覧を反映しない', async () => {
    const archivedConversation = {
      ...conversation,
      archived_at: '2026-08-01T13:00:00+00:00',
    }
    let archivedListRequestCount = 0
    let resolveStaleArchivedList: ((response: Response) => void) | undefined
    let deleteCompleted = false
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (init?.method === 'DELETE') {
        deleteCompleted = true
        return new Response(null, { status: 204 })
      }
      if (url.endsWith('/archived')) {
        archivedListRequestCount += 1
        if (archivedListRequestCount === 1) {
          return new Response(JSON.stringify([archivedConversation]), { status: 200 })
        }
        if (archivedListRequestCount === 2) {
          return new Promise<Response>((resolve) => { resolveStaleArchivedList = resolve })
        }
        return new Response(JSON.stringify(deleteCompleted ? [] : [archivedConversation]), {
          status: 200,
        })
      }
      if (url.endsWith('/conversations')) return new Response('[]', { status: 200 })
      return defaultFetch(input, init)
    })
    render(App)
    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))
    await screen.findByRole('button', { name: `削除 ${CONVERSATION_ID}` })
    await fireEvent.click(screen.getByRole('button', { name: 'アクティブ' }))
    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))
    await waitFor(() => expect(resolveStaleArchivedList).toBeDefined())

    await fireEvent.click(screen.getByRole('button', { name: `削除 ${CONVERSATION_ID}` }))
    await fireEvent.click(screen.getByRole('button', { name: '完全に削除' }))
    await waitFor(() => expect(
      screen.queryByRole('button', { name: CONVERSATION_ID }),
    ).toBeNull())

    if (resolveStaleArchivedList === undefined) {
      throw new Error('Stale archived list resolver is required')
    }
    const resolveList = resolveStaleArchivedList
    await act(() => resolveList(new Response(
      JSON.stringify([archivedConversation]),
      { status: 200 },
    )))

    expect(screen.queryByRole('button', { name: CONVERSATION_ID })).toBeNull()
  })

  test('archive API失敗時は一覧・履歴・選択状態を維持する', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (init?.method === 'POST' && url.endsWith('/archive')) {
        return new Response(null, { status: 503 })
      }
      return defaultFetch(input, init)
    })
    render(App)
    await selectConversation()
    await screen.findByText('保存済みの回答')

    await fireEvent.click(screen.getByRole('button', { name: `アーカイブ ${CONVERSATION_ID}` }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByRole('button', { name: CONVERSATION_ID })).toBeTruthy()
    expect(screen.getByText('保存済みの回答')).toBeTruthy()
    expect(localStorage.getItem('digital-souls:conversation:miori')).toBe(CONVERSATION_ID)
  })

  test('unarchive API失敗時はarchived一覧を維持する', async () => {
    const archivedConversation = {
      ...conversation,
      archived_at: '2026-08-01T13:00:00+00:00',
    }
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (init?.method === 'POST' && url.endsWith('/unarchive')) {
        return new Response(null, { status: 503 })
      }
      if (url.endsWith('/archived')) {
        return new Response(JSON.stringify([archivedConversation]), { status: 200 })
      }
      if (url.endsWith('/conversations')) return new Response('[]', { status: 200 })
      return defaultFetch(input, init)
    })
    render(App)
    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))

    await fireEvent.click(await screen.findByRole('button', { name: `復元 ${CONVERSATION_ID}` }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByRole('button', { name: CONVERSATION_ID })).toBeTruthy()
  })

  test('hard delete API失敗時は対象一覧・選択保存・確認状態を維持する', async () => {
    const archivedConversation = {
      ...conversation,
      archived_at: '2026-08-01T13:00:00+00:00',
    }
    localStorage.setItem('digital-souls:conversation:miori', CONVERSATION_ID)
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (init?.method === 'DELETE') return new Response(null, { status: 503 })
      if (url.endsWith('/archived')) {
        return new Response(JSON.stringify([archivedConversation]), { status: 200 })
      }
      if (url.endsWith('/conversations')) return new Response('[]', { status: 200 })
      return defaultFetch(input, init)
    })
    render(App)
    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))
    await fireEvent.click(await screen.findByRole('button', { name: `削除 ${CONVERSATION_ID}` }))

    await fireEvent.click(screen.getByRole('button', { name: '完全に削除' }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(screen.getByRole('button', { name: CONVERSATION_ID })).toBeTruthy()
    expect(localStorage.getItem('digital-souls:conversation:miori')).toBe(CONVERSATION_ID)
  })

  test('character切替時に前characterの履歴を即時に消去する', async () => {
    render(App)
    await fireEvent.click(await screen.findByRole('button', { name: new RegExp(`^${CONVERSATION_ID}$`) }))
    await screen.findByText('保存済みの回答')

    await fireEvent.input(screen.getByLabelText('キャラクターID'), { target: { value: 'akira' } })
    await fireEvent.click(screen.getByRole('button', { name: '切り替え' }))

    await waitFor(() => expect(screen.queryByText('保存済みの回答')).toBeNull())
  })

  test('character切替後に到着した旧characterのarchived一覧を反映しない', async () => {
    const archivedConversation = {
      ...conversation,
      conversation_id: '6ad9a610-02cc-4a41-b02e-503826f7292b',
      archived_at: '2026-08-01T12:02:00+00:00',
    }
    let resolveMioriArchived: ((response: Response) => void) | undefined
    const fetchMock = vi.mocked(fetch)
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/characters/miori/conversations/archived')) {
        return new Promise<Response>((resolve) => { resolveMioriArchived = resolve })
      }
      if (url.includes('/archived')) return new Response('[]', { status: 200 })
      if (url.includes('/api/characters/akira/conversations')) return new Response('[]', { status: 200 })
      return new Response(JSON.stringify([conversation]), { status: 200 })
    })
    render(App)
    await screen.findByRole('button', { name: new RegExp(`^${CONVERSATION_ID}$`) })

    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))
    await fireEvent.input(screen.getByLabelText('キャラクターID'), { target: { value: 'akira' } })
    await fireEvent.click(screen.getByRole('button', { name: '切り替え' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/characters/akira/conversations',
      undefined,
    ))
    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))
    resolveMioriArchived?.(
      new Response(JSON.stringify([archivedConversation]), { status: 200 }),
    )

    await waitFor(() => {
      expect(screen.queryByText(archivedConversation.conversation_id)).toBeNull()
    })
  })


  test('HTTP応答待機中はスレッド切替を防ぎ完了後に許可する', async () => {
    const secondConversation = {
      ...conversation,
      conversation_id: SECOND_CONVERSATION_ID,
    }
    let resolveChat: ((response: Response) => void) | undefined
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/api/chat') {
        return new Promise<Response>((resolve) => { resolveChat = resolve })
      }
      if (url.endsWith(`/${SECOND_CONVERSATION_ID}/turns`)) {
        return new Response(JSON.stringify([
          persistedTurn('切替先の質問', '切替先の回答'),
        ]), { status: 200 })
      }
      if (url.endsWith('/turns') || url.endsWith('/archived')) {
        return defaultFetch(input, init)
      }
      return new Response(JSON.stringify([conversation, secondConversation]), { status: 200 })
    })
    render(App)
    await fireEvent.click(await screen.findByRole('button', { name: CONVERSATION_ID }))
    await screen.findByText('保存済みの回答')
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '遅延する質問' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(screen.getByRole<HTMLButtonElement>('button', { name: SECOND_CONVERSATION_ID }).disabled).toBe(true)
    await fireEvent.click(screen.getByRole('button', { name: SECOND_CONVERSATION_ID }))
    expect(screen.getByText('保存済みの回答')).toBeTruthy()
    if (resolveChat === undefined) throw new Error('Chat response resolver is required')
    resolveChat(new Response(JSON.stringify({
      character: 'miori',
      turn: persistedTurn('遅延する質問', '切替前スレッドの遅延応答'),
    }), { status: 200 }))

    expect(await screen.findByText('切替前スレッドの遅延応答')).toBeTruthy()
    await waitFor(() => expect(
      screen.getByRole<HTMLButtonElement>('button', { name: SECOND_CONVERSATION_ID }).disabled,
    ).toBe(false))
    await fireEvent.click(screen.getByRole('button', { name: SECOND_CONVERSATION_ID }))
    expect(await screen.findByText('切替先の回答')).toBeTruthy()
  })

  test('スレッド切替後に失敗した切替元の履歴取得エラーを表示しない', async () => {
    const secondConversation = {
      ...conversation,
      conversation_id: SECOND_CONVERSATION_ID,
    }
    let rejectPreviousHistory: ((reason: Error) => void) | undefined
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith(`/${CONVERSATION_ID}/turns`)) {
        return new Promise<Response>((_resolve, reject) => { rejectPreviousHistory = reject })
      }
      if (url.endsWith(`/${SECOND_CONVERSATION_ID}/turns`)) {
        return new Response(JSON.stringify([
          persistedTurn('切替先の質問', '切替先の回答'),
        ]), { status: 200 })
      }
      if (url.endsWith('/turns') || url.endsWith('/archived')) return defaultFetch(input, init)
      return new Response(JSON.stringify([conversation, secondConversation]), { status: 200 })
    })
    render(App)
    await fireEvent.click(await screen.findByRole('button', { name: CONVERSATION_ID }))
    await waitFor(() => expect(rejectPreviousHistory).toBeDefined())

    await fireEvent.click(screen.getByRole('button', { name: SECOND_CONVERSATION_ID }))
    expect(await screen.findByText('切替先の回答')).toBeTruthy()
    if (rejectPreviousHistory === undefined) throw new Error('History rejection function is required')
    rejectPreviousHistory(new Error('previous history failed'))

    await waitFor(() => {
      expect(screen.queryByRole('alert')).toBeNull()
      expect(screen.getByText('切替先の回答')).toBeTruthy()
    })
  })



  test('HTTP送信失敗時にエラーを表示して入力を再び有効にする', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/api/chat') throw new Error('backend error')
      return defaultFetch(input, init)
    })
    render(App)
    await selectConversation()

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '応答して' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect((await screen.findByRole('alert')).textContent).toBe('応答の取得に失敗しました。')
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(false)
  })











  test('text応答待機中も継続音声入力のマイク操作は維持する', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/api/chat') return new Promise<Response>(() => {})
      return defaultFetch(input, init)
    })
    render(App)
    await selectConversation()
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '少し待って' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'マイクをオンにする' }).disabled).toBe(false)
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'キャラクターID' }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '切り替え' }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: CONVERSATION_ID }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: `アーカイブ ${CONVERSATION_ID}` }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '新規スレッド' }).disabled).toBe(true)
  })

  test('物理削除の確認中は他操作を無効にし開始元characterを削除対象にする', async () => {
    const archivedConversation = {
      ...conversation,
      archived_at: '2026-08-01T12:02:00+00:00',
    }
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/archived')) {
        return new Response(JSON.stringify([archivedConversation]), { status: 200 })
      }
      return defaultFetch(input, init)
    })
    render(App)
    await screen.findByRole('button', { name: CONVERSATION_ID })
    await fireEvent.click(screen.getByRole('button', { name: 'アーカイブ済み' }))
    await fireEvent.click(await screen.findByRole('button', { name: `削除 ${CONVERSATION_ID}` }))

    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'キャラクターID' }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '切り替え' }).disabled).toBe(true)
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'アクティブ' }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: `復元 ${CONVERSATION_ID}` }).disabled).toBe(true)

    await fireEvent.click(screen.getByRole('button', { name: '完全に削除' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      `/api/characters/miori/conversations/${CONVERSATION_ID}`,
      { method: 'DELETE' },
    ))
  })




})
