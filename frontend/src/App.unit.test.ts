import { act, fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import App from './App.svelte'

const CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const SECOND_CONVERSATION_ID = '6ad9a610-02cc-4a41-b02e-503826f7292b'
const THIRD_CONVERSATION_ID = 'f98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const TURN_ID = '9e70795d-e5d5-431d-baa2-67f884403010'

const conversation = {
  character_id: 'miori',
  conversation_id: CONVERSATION_ID,
  created_at: '2026-08-01T12:00:00+00:00',
  updated_at: '2026-08-01T12:01:00+00:00',
  archived_at: null,
}

class FakeWebSocket {
  static instances: FakeWebSocket[] = []

  binaryType = ''
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  sent: (string | ArrayBuffer)[] = []
  closeCalls = 0

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this)
  }

  send(data: string | ArrayBuffer) { this.sent.push(data) }
  close() {
    this.closeCalls += 1
    this.onclose?.()
  }
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
    | { onSpeechStart: () => void; onSpeechEnd: () => void }
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

const latestSocket = (): FakeWebSocket => {
  const socket = FakeWebSocket.instances.at(-1)
  if (socket === undefined) throw new Error('WebSocket instance is required')
  return socket
}

const selectConversation = async (): Promise<FakeWebSocket> => {
  await fireEvent.click(
    await screen.findByRole('button', { name: new RegExp(`^${CONVERSATION_ID}$`) }),
  )
  await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
  return latestSocket()
}

const openSocket = async (): Promise<FakeWebSocket> => {
  const socket = await selectConversation()
  await act(() => socket.onopen?.())
  return socket
}

const audioTurnFrame = (userContent: string, assistantContent: string): MessageEvent => (
  new MessageEvent('message', {
    data: JSON.stringify({ type: 'text', turn: persistedTurn(userContent, assistantContent) }),
  })
)

describe('App conversation lifecycle', () => {
  beforeEach(() => {
    localStorage.clear()
    window.history.replaceState({}, '', '/')
    FakeWebSocket.instances = []
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
    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => TURN_ID) })
    fetchMock.mockReset().mockImplementation(async (input, init) => defaultFetch(input, init))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('AudioContext', FakeAudioContext)
    vi.stubGlobal('navigator', {
      mediaDevices: { getUserMedia: audioMocks.getUserMedia },
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

  test('履歴取得中に受信したWebSocket turnを履歴応答後も重複なく維持する', async () => {
    const receivedTurn = persistedTurn('取得中の音声質問', '取得中の音声回答')
    const historicalTurn = persistedTurn('過去の質問', '過去の回答')
    let resolveHistory: ((response: Response) => void) | undefined
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/turns')) {
        return new Promise<Response>((resolve) => { resolveHistory = resolve })
      }
      return defaultFetch(input, init)
    })
    render(App)
    const socket = await selectConversation()
    await waitFor(() => expect(resolveHistory).toBeDefined())

    socket.onmessage?.(new MessageEvent('message', {
      data: JSON.stringify({ type: 'text', turn: receivedTurn }),
    }))
    expect(await screen.findByText('取得中の音声回答')).toBeTruthy()
    if (resolveHistory === undefined) throw new Error('History resolver is required')
    const resolveTurns = resolveHistory
    await act(() => resolveTurns(new Response(
      JSON.stringify([historicalTurn, receivedTurn]),
      { status: 200 },
    )))

    expect(await screen.findByText('過去の回答')).toBeTruthy()
    expect(screen.getAllByText('取得中の音声質問')).toHaveLength(1)
    expect(screen.getAllByText('取得中の音声回答')).toHaveLength(1)
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

  test('同じconversation IDを音声WebSocketとHTTP送信へ使用する', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: 'こんにちは' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.url).toBe(`ws://localhost:3000/ws/miori?conversation_id=${CONVERSATION_ID}`)
    const chatCall = fetchMock.mock.calls.find(([url]) => String(url) === '/api/chat')
    expect(JSON.parse(String(chatCall?.[1]?.body))).toEqual({
      character: 'miori',
      conversation_id: CONVERSATION_ID,
      message: 'こんにちは',
    })
    expect(await screen.findByText('HTTP応答です。')).toBeTruthy()
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

  test('音声応答待機中はスレッド操作を無効にする', async () => {
    const secondConversation = {
      ...conversation,
      conversation_id: SECOND_CONVERSATION_ID,
    }
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith(`/${SECOND_CONVERSATION_ID}/turns`)) {
        return new Response(JSON.stringify([
          persistedTurn('切替先の質問', '切替先の回答'),
        ]), { status: 200 })
      }
      if (url.endsWith('/turns') || url.endsWith('/archived')) return defaultFetch(input, init)
      return new Response(JSON.stringify([conversation, secondConversation]), { status: 200 })
    })
    render(App)
    const socket = await openSocket()
    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(audioMocks.vadStart).toHaveBeenCalledTimes(1))
    if (audioMocks.vadOptions === undefined) throw new Error('VAD callbacks are required')
    audioMocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([audioMocks.pcmData]))

    expect(screen.getByRole<HTMLButtonElement>('button', { name: SECOND_CONVERSATION_ID }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: `アーカイブ ${CONVERSATION_ID}` }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '新規スレッド' }).disabled).toBe(true)
    expect(FakeWebSocket.instances).toHaveLength(1)
  })

  test('characterをAからBからAへ切り替えても各conversation IDを混同しない', async () => {
    const conversationIdB = SECOND_CONVERSATION_ID
    const conversationB = {
      ...conversation,
      character_id: 'akira',
      conversation_id: conversationIdB,
    }
    localStorage.setItem('digital-souls:conversation:miori', CONVERSATION_ID)
    localStorage.setItem('digital-souls:conversation:akira', conversationIdB)
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input)
      if (url === '/api/chat') return defaultFetch(input, init)
      if (url.endsWith('/turns') || url.endsWith('/archived')) {
        return new Response('[]', { status: 200 })
      }
      if (url.includes('/characters/akira/')) {
        return new Response(JSON.stringify([conversationB]), { status: 200 })
      }
      return new Response(JSON.stringify([conversation]), { status: 200 })
    })
    render(App)
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    await act(() => latestSocket().onopen?.())
    const switcher = screen.getByRole('textbox', { name: 'キャラクターID' })

    await fireEvent.input(switcher, { target: { value: 'akira' } })
    await fireEvent.click(screen.getByRole('button', { name: '切り替え' }))
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))
    await act(() => latestSocket().onopen?.())
    await fireEvent.input(switcher, { target: { value: 'miori' } })
    await fireEvent.click(screen.getByRole('button', { name: '切り替え' }))
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(3))

    expect(FakeWebSocket.instances.map((socket) => socket.url)).toEqual([
      `ws://localhost:3000/ws/miori?conversation_id=${CONVERSATION_ID}`,
      `ws://localhost:3000/ws/akira?conversation_id=${conversationIdB}`,
      `ws://localhost:3000/ws/miori?conversation_id=${CONVERSATION_ID}`,
    ])
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

  test('音声応答待機中のWebSocketエラーでpending状態を解除する', async () => {
    render(App)
    const socket = await openSocket()
    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(audioMocks.vadStart).toHaveBeenCalledTimes(1))
    if (audioMocks.vadOptions === undefined) throw new Error('VAD callbacks are required')
    audioMocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([audioMocks.pcmData]))

    socket.onerror?.()

    expect((await screen.findByRole('alert')).textContent).toBe('応答の取得に失敗しました。')
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(false)
  })

  test('HTTP応答待機中のWebSocketエラーではtext pending状態を維持する', async () => {
    let resolveChat: ((response: Response) => void) | undefined
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/api/chat') {
        return new Promise<Response>((resolve) => { resolveChat = resolve })
      }
      return defaultFetch(input, init)
    })
    render(App)
    const socket = await openSocket()
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '待機中' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    socket.onerror?.()

    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(true)
    resolveChat?.(new Response(JSON.stringify({
      character: 'miori',
      turn: persistedTurn('待機中', 'HTTP完了'),
    }), { status: 200 }))
    expect(await screen.findByText('HTTP完了')).toBeTruthy()
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(false)
  })

  test('WebSocket接続前はマイクを無効にするがテキスト入力は許可する', async () => {
    render(App)
    await selectConversation()
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '接続前に送信' },
    })

    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'マイクをオンにする' }).disabled).toBe(true)
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(false)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '送信' }).disabled).toBe(false)
  })

  test('初回WebSocket接続失敗後もテキスト操作を維持する', async () => {
    render(App)
    const socket = await selectConversation()

    await act(() => socket.onerror?.())

    expect((await screen.findByRole('alert')).textContent).toBe('応答の取得に失敗しました。')
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(false)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'マイクをオンにする' }).disabled).toBe(true)
  })

  test('録音音声を送信し保存済みturnを表示してWAVを再生する', async () => {
    render(App)
    const socket = await openSocket()
    const wav = new ArrayBuffer(12)
    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(audioMocks.vadStart).toHaveBeenCalledTimes(1))
    if (audioMocks.vadOptions === undefined) throw new Error('VAD callbacks are required')
    audioMocks.vadOptions.onSpeechStart()
    audioMocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([audioMocks.pcmData]))

    socket.onmessage?.(audioTurnFrame('明日の予定を教えて', '明日は午前中が空いています。'))
    socket.onmessage?.(new MessageEvent('message', { data: wav }))

    expect(await screen.findByText('明日の予定を教えて')).toBeTruthy()
    expect(screen.getByText('明日は午前中が空いています。')).toBeTruthy()
    await waitFor(() => expect(decodeAudioData).toHaveBeenCalledWith(wav.slice(0)))
    expect(start).toHaveBeenCalledTimes(1)
  })

  test('保存済みturn到着後も音声バイナリ到着までは入力を無効にする', async () => {
    render(App)
    const socket = await openSocket()
    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(audioMocks.vadStart).toHaveBeenCalledTimes(1))
    if (audioMocks.vadOptions === undefined) throw new Error('VAD callbacks are required')
    audioMocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toHaveLength(1))

    socket.onmessage?.(audioTurnFrame('音声の途中です', '再生準備中です。'))

    expect(await screen.findByText('再生準備中です。')).toBeTruthy()
    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'マイクをオフにする' }).classList).toContain('mic-standby')
  })

  test('音声応答待機中はマイクをstandbyのまま維持する', async () => {
    render(App)
    const socket = await openSocket()
    const closeCallCount = audioMocks.recorderClose.mock.calls.length
    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(audioMocks.vadStart).toHaveBeenCalledTimes(1))
    if (audioMocks.vadOptions === undefined) throw new Error('VAD callbacks are required')
    audioMocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toHaveLength(1))

    const microphone = screen.getByRole<HTMLButtonElement>('button', { name: 'マイクをオフにする' })
    expect(microphone.disabled).toBe(true)
    expect(microphone.getAttribute('aria-pressed')).toBe('true')
    expect(audioMocks.recorderClose).toHaveBeenCalledTimes(closeCallCount)
  })

  test('WebSocket切断時にマイクを強制的にオフにする', async () => {
    render(App)
    const socket = await openSocket()
    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(audioMocks.vadStart).toHaveBeenCalledTimes(1))

    socket.onclose?.()

    await waitFor(() => expect(audioMocks.recorderClose).toHaveBeenCalledTimes(1))
    expect(audioMocks.vadDestroy).toHaveBeenCalledTimes(1)
  })

  test('text応答待機中は入力・マイク・character切替を無効にする', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/api/chat') return new Promise<Response>(() => {})
      return defaultFetch(input, init)
    })
    render(App)
    await openSocket()
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '少し待って' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'マイクをオンにする' }).disabled).toBe(true)
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

  test('text応答待機中にWebSocketが閉じてもtext入力を無効のままにする', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input) === '/api/chat') return new Promise<Response>(() => {})
      return defaultFetch(input, init)
    })
    render(App)
    const socket = await openSocket()
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '閉じても待って' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    socket.onclose?.()

    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(true)
  })

  test('音声応答待機中はtext送信を防ぎ、音声完了後に許可する', async () => {
    render(App)
    const socket = await openSocket()
    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(audioMocks.vadStart).toHaveBeenCalledTimes(1))
    if (audioMocks.vadOptions === undefined) throw new Error('VAD callbacks are required')
    audioMocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toHaveLength(1))

    expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(true)
    socket.onmessage?.(audioTurnFrame('音声の質問', '音声の応答です。'))
    socket.onmessage?.(new MessageEvent('message', { data: new ArrayBuffer(12) }))
    await waitFor(() => expect(screen.getByRole<HTMLInputElement>('textbox', { name: 'メッセージ' }).disabled).toBe(false))
  })

  test('最初の音声応答が完了するまではマイク操作を無効にする', async () => {
    render(App)
    const socket = await openSocket()
    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(audioMocks.vadStart).toHaveBeenCalledTimes(1))
    if (audioMocks.vadOptions === undefined) throw new Error('VAD callbacks are required')
    audioMocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toHaveLength(1))
    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'マイクをオフにする' }).disabled).toBe(true)

    socket.onmessage?.(new MessageEvent('message', { data: new ArrayBuffer(12) }))

    await waitFor(() => expect(screen.getByRole<HTMLButtonElement>('button', { name: 'マイクをオフにする' }).disabled).toBe(false))
  })

  test('受信音声をデコードできない場合にエラーを表示する', async () => {
    decodeAudioData.mockRejectedValueOnce(new Error('decode failed'))
    render(App)
    const socket = await openSocket()
    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(audioMocks.vadStart).toHaveBeenCalledTimes(1))
    if (audioMocks.vadOptions === undefined) throw new Error('VAD callbacks are required')
    audioMocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toHaveLength(1))

    socket.onmessage?.(new MessageEvent('message', { data: new ArrayBuffer(12) }))

    expect((await screen.findByRole('alert')).textContent).toBe('応答の取得に失敗しました。')
    expect(start).not.toHaveBeenCalled()
  })
})
