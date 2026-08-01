import { act, fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import App from './App.svelte'

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

  send(data: string | ArrayBuffer) {
    this.sent.push(data)
  }

  close() {
    this.closeCalls += 1
    this.onclose?.()
  }
}

const mocks = vi.hoisted(() => ({
  pcmData: new ArrayBuffer(4),
  vadStart: vi.fn(),
  vadDestroy: vi.fn(),
  recorderInitialize: vi.fn(),
  recorderStart: vi.fn(),
  recorderStopAndTake: vi.fn(),
  recorderClose: vi.fn(),
  getUserMedia: vi.fn(),
  microphoneStream: {
    getTracks: () => [],
  } as unknown as MediaStream,
  vadOptions: undefined as
    | {
        baseAssetPath: string
        onnxWASMBasePath: string
        getStream: () => Promise<MediaStream>
        resumeStream: (stream: MediaStream) => Promise<MediaStream>
        pauseStream: (stream: MediaStream) => Promise<void>
        startOnLoad: boolean
        onSpeechStart: () => void
        onSpeechEnd: () => void
      }
    | undefined,
}))

vi.mock('@ricky0123/vad-web', () => ({
  MicVAD: {
    new: vi.fn(async (options) => {
      mocks.vadOptions = options
      return {
        start: mocks.vadStart,
        destroy: mocks.vadDestroy,
      }
    }),
  },
}))

vi.mock('./lib/audio/pcm-worklet-recorder', () => ({
  AudioWorkletPcmRecorder: vi.fn(() => ({
    initialize: mocks.recorderInitialize,
    start: mocks.recorderStart,
    stopAndTake: mocks.recorderStopAndTake,
    close: mocks.recorderClose,
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

const latestSocket = (): FakeWebSocket => {
  const socket = FakeWebSocket.instances[FakeWebSocket.instances.length - 1]

  if (socket === undefined) {
    throw new Error('WebSocket instance is required')
  }

  return socket
}

const findRenderedMessage = (container: HTMLElement, text: string): HTMLElement => {
  const messages = Array.from(container.querySelectorAll<HTMLElement>('.message'))
  const message = messages.find((element) => element.textContent?.includes(text))

  if (message === undefined) {
    throw new Error(`Rendered message is required: ${text}`)
  }

  return message
}

const openSocket = async (): Promise<FakeWebSocket> => {
  await act()
  await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
  const socket = latestSocket()
  socket.onopen?.()
  return socket
}

describe('App chat and audio flow', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    window.history.replaceState({}, '', '/')
    localStorage.clear()
    FakeWebSocket.instances = []
    mocks.vadOptions = undefined
    mocks.vadStart.mockReset()
    mocks.vadStart.mockResolvedValue(undefined)
    mocks.vadDestroy.mockReset()
    mocks.vadDestroy.mockResolvedValue(undefined)
    mocks.recorderInitialize.mockReset()
    mocks.recorderInitialize.mockResolvedValue(undefined)
    mocks.recorderStart.mockReset()
    mocks.recorderStopAndTake.mockReset()
    mocks.recorderStopAndTake.mockResolvedValue(mocks.pcmData)
    mocks.recorderClose.mockReset()
    mocks.recorderClose.mockResolvedValue(undefined)
    mocks.getUserMedia.mockReset()
    mocks.getUserMedia.mockResolvedValue(mocks.microphoneStream)
    decodeAudioData.mockReset()
    createBufferSource.mockReset()
    connect.mockReset()
    start.mockReset()
    close.mockReset()
    close.mockResolvedValue(undefined)
    createBufferSource.mockReturnValue({ connect, start })
    decodeAudioData.mockResolvedValue({ duration: 1 })
    vi.stubGlobal('WebSocket', FakeWebSocket)
    fetchMock.mockReset()
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ character: 'miori', response: 'HTTP応答です。' }), {
        status: 200,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', {
      randomUUID: vi.fn(() => 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'),
    })
    vi.stubGlobal('AudioContext', FakeAudioContext)
    vi.stubGlobal('navigator', {
      mediaDevices: {
        getUserMedia: mocks.getUserMedia,
      },
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  test('should use one conversation ID for the voice WebSocket and HTTP text request', async () => {
    const { container } = render(App)
    const socket = await openSocket()

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: 'こんにちは' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.url).toBe(
      'ws://localhost:3000/ws/miori?conversation_id=e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010',
    )
    expect(socket.sent).toEqual([])
    expect(await screen.findByText('こんにちは')).toBeTruthy()
    expect(await screen.findByText('HTTP応答です。')).toBeTruthy()
    const [, request] = fetchMock.mock.calls[0] ?? []
    expect(JSON.parse(String(request?.body))).toEqual({
      character: 'miori',
      conversation_id: 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010',
      message: 'こんにちは',
    })
    if (container.textContent === null) {
      throw new Error('Chat text content is required')
    }
    expect(container.textContent.indexOf('こんにちは')).toBeLessThan(
      container.textContent.indexOf('HTTP応答です。'),
    )
  })

  test('should route A to B to A through HTTP and WebSocket without mixing conversation IDs', async () => {
    const conversationIdA = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
    const conversationIdB = '6ad9a610-02cc-4a41-b02e-503826f7292b'
    vi.stubGlobal('crypto', {
      randomUUID: vi.fn()
        .mockReturnValueOnce(conversationIdA)
        .mockReturnValueOnce(conversationIdB),
    })
    fetchMock.mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body)) as Record<string, string>
      return new Response(
        JSON.stringify({ character: body.character, response: `${body.character}の応答` }),
        { status: 200 },
      )
    })
    render(App)
    const socketA = await openSocket()

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: 'Aへの質問' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))
    expect(await screen.findByText('mioriの応答')).toBeTruthy()

    const switcher = screen.getByRole('textbox', { name: 'キャラクターID' })
    await fireEvent.input(switcher, { target: { value: 'mock character/b' } })
    await fireEvent.click(screen.getByRole('button', { name: '切り替え' }))
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2))
    const socketB = latestSocket()
    await act(() => socketB.onopen?.())

    expect(socketA.closeCalls).toBe(1)
    socketA.onclose?.()
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      false,
    )
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: 'Bへの質問' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))
    expect(await screen.findByText('mock character/bの応答')).toBeTruthy()

    await fireEvent.input(switcher, { target: { value: 'miori' } })
    await fireEvent.click(screen.getByRole('button', { name: '切り替え' }))
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(3))
    const returnedSocketA = latestSocket()
    await act(() => returnedSocketA.onopen?.())

    expect(socketB.closeCalls).toBe(1)
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: 'Aへの再質問' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    const requestBodies = fetchMock.mock.calls.map(([, init]) => (
      JSON.parse(String(init?.body)) as Record<string, string>
    ))
    expect(requestBodies).toEqual([
      { character: 'miori', conversation_id: conversationIdA, message: 'Aへの質問' },
      {
        character: 'mock character/b',
        conversation_id: conversationIdB,
        message: 'Bへの質問',
      },
      { character: 'miori', conversation_id: conversationIdA, message: 'Aへの再質問' },
    ])
    expect(FakeWebSocket.instances.map((socket) => socket.url)).toEqual([
      `ws://localhost:3000/ws/miori?conversation_id=${conversationIdA}`,
      `ws://localhost:3000/ws/mock%20character%2Fb?conversation_id=${conversationIdB}`,
      `ws://localhost:3000/ws/miori?conversation_id=${conversationIdA}`,
    ])
    expect(conversationIdB).not.toBe(conversationIdA)
  })

  test('should render an error message when the HTTP text request fails', async () => {
    fetchMock.mockRejectedValueOnce(new Error('backend error'))
    render(App)
    await openSocket()

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '応答して' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(await screen.findByText('応答して')).toBeTruthy()
    expect(await screen.findByText('応答の取得に失敗しました。')).toBeTruthy()
  })

  test('should render an error message and clear pending state when the WebSocket runtime errors', async () => {
    render(App)
    const socket = await openSocket()

    const microphoneButton = screen.getByRole('button', { name: 'マイクをオンにする' })
    microphoneButton.click()
    await waitFor(() => expect(mocks.vadStart).toHaveBeenCalledTimes(1))

    if (mocks.vadOptions === undefined) {
      throw new Error('VAD callbacks are required')
    }
    mocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([mocks.pcmData]))

    socket.onerror?.()

    expect(await screen.findByText('応答の取得に失敗しました。')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(false)
  })

  test('should keep HTTP text pending state when the WebSocket runtime errors', async () => {
    let resolveRequest: ((response: Response) => void) | undefined
    fetchMock.mockImplementationOnce(
      () => new Promise<Response>((resolve) => { resolveRequest = resolve }),
    )
    render(App)
    const socket = await openSocket()

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: 'エラー後も入力したい' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.sent).toEqual([])
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)

    socket.onerror?.()

    expect(await screen.findByText('応答の取得に失敗しました。')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )
    resolveRequest?.(
      new Response(JSON.stringify({ character: 'miori', response: 'HTTP完了' }), { status: 200 }),
    )
    expect(await screen.findByText('HTTP完了')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(false)
  })

  test('should disable microphone capture before the WebSocket is connected', async () => {
    render(App)

    const microphoneButton = screen.getByRole('button', { name: 'マイクをオンにする' })

    expect(microphoneButton.hasAttribute('disabled')).toBe(true)
    expect(mocks.vadStart).not.toHaveBeenCalled()
  })

  test('should allow HTTP text chat before the WebSocket connects', async () => {
    render(App)
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '接続前に送信' },
    })

    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: '送信' }).hasAttribute('disabled')).toBe(false)
  })

  test('should render an error and keep HTTP text controls enabled when the initial WebSocket connection fails', async () => {
    render(App)
    await act()
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1))
    const socket = latestSocket()

    await act(async () => {
      socket.onerror?.()
    })

    expect(await screen.findByText('応答の取得に失敗しました。')).toBeTruthy()
    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '音声接続失敗後に送信' },
    })
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: '送信' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )
  })

  test('should send captured microphone audio and play the backend WAV response', async () => {
    const { container } = render(App)
    const socket = await openSocket()
    const wav = new ArrayBuffer(12)

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(mocks.getUserMedia).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(mocks.recorderInitialize).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(mocks.vadStart).toHaveBeenCalledTimes(1))
    expect(mocks.recorderInitialize).toHaveBeenCalledWith(mocks.microphoneStream)

    if (mocks.vadOptions === undefined) {
      throw new Error('VAD callbacks are required')
    }
    mocks.vadOptions.onSpeechStart()
    mocks.vadOptions.onSpeechEnd()

    expect(mocks.recorderStart).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(socket.sent).toEqual([mocks.pcmData]))
    expect(screen.getByRole('button', { name: 'マイクをオフにする' }).hasAttribute('disabled')).toBe(
      true,
    )
    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', speaker: 'user', message: '明日の予定を教えて' }),
      }),
    )
    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', speaker: 'miori', response: '明日は午前中が空いています。' }),
      }),
    )

    const userMessage = await screen.findByText('明日の予定を教えて')
    const mioriMessage = await screen.findByText('明日は午前中が空いています。')
    expect(userMessage).toBeTruthy()
    expect(mioriMessage).toBeTruthy()
    expect(findRenderedMessage(container, '明日の予定を教えて').textContent).toContain('あなた')
    expect(findRenderedMessage(container, '明日は午前中が空いています。').textContent).toContain('光織')

    socket.onmessage?.(new MessageEvent('message', { data: wav }))

    await waitFor(() => expect(decodeAudioData).toHaveBeenCalledWith(wav.slice(0)))
    expect(createBufferSource).toHaveBeenCalledTimes(1)
    expect(connect).toHaveBeenCalledTimes(1)
    expect(start).toHaveBeenCalledTimes(1)
  })

  test('should keep text input disabled until the audio binary response arrives', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(mocks.vadStart).toHaveBeenCalledTimes(1))

    if (mocks.vadOptions === undefined) {
      throw new Error('VAD callbacks are required')
    }
    mocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([mocks.pcmData]))

    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', speaker: 'user', message: '音声の途中です' }),
      }),
    )
    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', speaker: 'miori', response: '再生準備中です。' }),
      }),
    )

    expect(await screen.findByText('音声の途中です')).toBeTruthy()
    expect(await screen.findByText('再生準備中です。')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)
    expect(mocks.recorderClose).not.toHaveBeenCalled()
    expect(mocks.vadDestroy).not.toHaveBeenCalled()
    const microphoneButton = screen.getByRole('button', { name: 'マイクをオフにする' })
    expect(microphoneButton.hasAttribute('disabled')).toBe(true)
    expect(microphoneButton.classList.contains('mic-standby')).toBe(true)
  })

  test('should keep microphone in standby while an audio response is pending', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(mocks.vadStart).toHaveBeenCalledTimes(1))

    if (mocks.vadOptions === undefined) {
      throw new Error('VAD callbacks are required')
    }
    mocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([mocks.pcmData]))

    const microphoneButton = screen.getByRole('button', { name: 'マイクをオフにする' })

    expect(microphoneButton.hasAttribute('disabled')).toBe(true)
    expect(microphoneButton.getAttribute('aria-pressed')).toBe('true')
    expect(microphoneButton.classList.contains('mic-standby')).toBe(true)
    expect(microphoneButton.classList.contains('mic-active')).toBe(false)
    expect(mocks.recorderClose).not.toHaveBeenCalled()
    expect(mocks.vadDestroy).not.toHaveBeenCalled()
  })

  test('should force microphone off when the WebSocket closes', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(mocks.vadStart).toHaveBeenCalledTimes(1))

    socket.onclose?.()

    await waitFor(() => expect(mocks.recorderClose).toHaveBeenCalledTimes(1))
    expect(mocks.vadDestroy).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )
  })

  test('should disable text, microphone, and character switching while a text response is pending', async () => {
    let resolveRequest: ((response: Response) => void) | undefined
    fetchMock.mockImplementationOnce(
      () => new Promise<Response>((resolve) => { resolveRequest = resolve }),
    )
    render(App)
    const socket = await openSocket()

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '少し待って' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.sent).toEqual([])
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )
    expect(
      screen.getByRole<HTMLInputElement>('textbox', { name: 'キャラクターID' }).disabled,
    ).toBe(true)
    expect(screen.getByRole<HTMLButtonElement>('button', { name: '切り替え' }).disabled).toBe(true)

    const disabledMicrophoneButton = screen.getByRole('button', { name: 'マイクをオンにする' })
    disabledMicrophoneButton.click()

    expect(mocks.getUserMedia).not.toHaveBeenCalled()
    expect(mocks.vadStart).not.toHaveBeenCalled()

    resolveRequest?.(
      new Response(JSON.stringify({ character: 'miori', response: 'お待たせしました。' }), {
        status: 200,
      }),
    )

    expect(await screen.findByText('お待たせしました。')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(false)
    expect(
      screen.getByRole<HTMLInputElement>('textbox', { name: 'キャラクターID' }).disabled,
    ).toBe(false)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      false,
    )
  })

  test('should keep text input disabled when the WebSocket closes', async () => {
    fetchMock.mockImplementationOnce(() => new Promise<Response>(() => {}))
    render(App)
    const socket = await openSocket()

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: '閉じても待って' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)

    socket.onclose?.()

    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )
  })

  test('should block text sends while an audio response is pending and allow them after audio completes', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(mocks.vadStart).toHaveBeenCalledTimes(1))

    if (mocks.vadOptions === undefined) {
      throw new Error('VAD callbacks are required')
    }
    mocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([mocks.pcmData]))

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: 'テキストもお願い' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.sent).toEqual([mocks.pcmData])
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)
    expect(mocks.recorderClose).not.toHaveBeenCalled()
    expect(mocks.vadDestroy).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'マイクをオフにする' }).hasAttribute('disabled')).toBe(true)

    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', speaker: 'user', message: '音声の質問' }),
      }),
    )
    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', speaker: 'miori', response: '音声の応答です。' }),
      }),
    )

    expect(await screen.findByText('音声の質問')).toBeTruthy()
    expect(await screen.findByText('音声の応答です。')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'マイクをオフにする' }).hasAttribute('disabled')).toBe(true)

    socket.onmessage?.(new MessageEvent('message', { data: new ArrayBuffer(12) }))

    await fireEvent.input(screen.getByRole('textbox', { name: 'メッセージ' }), {
      target: { value: 'テキストもお願い' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.sent).toEqual([mocks.pcmData])
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/chat',
      expect.objectContaining({ method: 'POST' }),
    )

    expect(await screen.findByText('HTTP応答です。')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: 'マイクをオフにする' }).hasAttribute('disabled')).toBe(false)
  })

  test('should disable microphone controls while the first audio response is pending', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(mocks.vadStart).toHaveBeenCalledTimes(1))

    if (mocks.vadOptions === undefined) {
      throw new Error('VAD callbacks are required')
    }
    mocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([mocks.pcmData]))

    expect(mocks.recorderClose).not.toHaveBeenCalled()
    expect(mocks.vadDestroy).not.toHaveBeenCalled()
    const microphoneButton = screen.getByRole('button', { name: 'マイクをオフにする' })
    expect(microphoneButton.hasAttribute('disabled')).toBe(true)
    expect(microphoneButton.classList.contains('mic-standby')).toBe(true)
  })

  test('should keep microphone disabled until the first audio response completes', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(mocks.vadStart).toHaveBeenCalledTimes(1))

    if (mocks.vadOptions === undefined) {
      throw new Error('VAD callbacks are required')
    }
    mocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([mocks.pcmData]))

    expect(mocks.recorderClose).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'マイクをオフにする' }).hasAttribute('disabled')).toBe(true)

    socket.onmessage?.(new MessageEvent('message', { data: new ArrayBuffer(12) }))

    expect(socket.sent).toEqual([mocks.pcmData])
    expect(screen.queryByText('応答の取得に失敗しました。')).toBeNull()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'マイクをオフにする' }).hasAttribute('disabled')).toBe(
        false,
      ),
    )
  })

  test('should render an error message when received audio cannot be decoded', async () => {
    const error = new Error('decode failed')
    decodeAudioData.mockRejectedValueOnce(error)
    render(App)
    const socket = await openSocket()

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(mocks.vadStart).toHaveBeenCalledTimes(1))

    if (mocks.vadOptions === undefined) {
      throw new Error('VAD callbacks are required')
    }
    mocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([mocks.pcmData]))

    socket.onmessage?.(new MessageEvent('message', { data: new ArrayBuffer(12) }))

    expect(await screen.findByText('応答の取得に失敗しました。')).toBeTruthy()
    expect(start).not.toHaveBeenCalled()
  })
})
