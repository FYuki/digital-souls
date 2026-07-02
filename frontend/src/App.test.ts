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

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this)
  }

  send(data: string | ArrayBuffer) {
    this.sent.push(data)
  }

  close() {
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

  test('should connect the audio transport on mount and send user text through the WebSocket', async () => {
    const { container } = render(App)
    const socket = await openSocket()

    await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'こんにちは' } })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.url).toBe('ws://localhost:3000/ws/miori')
    expect(socket.sent).toEqual([JSON.stringify({ type: 'text', message: 'こんにちは' })])
    expect(await screen.findByText('こんにちは')).toBeTruthy()

    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', response: '夕焼けがきれいですね。' }),
      }),
    )

    expect(await screen.findByText('夕焼けがきれいですね。')).toBeTruthy()
    if (container.textContent === null) {
      throw new Error('Chat text content is required')
    }
    expect(container.textContent.indexOf('こんにちは')).toBeLessThan(
      container.textContent.indexOf('夕焼けがきれいですね。'),
    )
  })

  test('should render legacy WebSocket text responses as miori messages', async () => {
    render(App)
    const socket = await openSocket()

    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', response: '従来形式の応答です。' }),
      }),
    )

    expect(await screen.findByText('従来形式の応答です。')).toBeTruthy()
  })

  test('should render an error message when the WebSocket text request fails', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.input(screen.getByRole('textbox'), { target: { value: '応答して' } })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'error', status: 500, detail: 'backend error' }),
      }),
    )

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

  test('should clear text pending state when the WebSocket runtime errors after a text send', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'エラー後も入力したい' } })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.sent).toEqual([JSON.stringify({ type: 'text', message: 'エラー後も入力したい' })])
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)

    socket.onerror?.()

    expect(await screen.findByText('応答の取得に失敗しました。')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      false,
    )
  })

  test('should disable microphone capture before the WebSocket is connected', async () => {
    render(App)

    const microphoneButton = screen.getByRole('button', { name: 'マイクをオンにする' })

    expect(microphoneButton.hasAttribute('disabled')).toBe(true)
    expect(mocks.vadStart).not.toHaveBeenCalled()
  })

  test('should disable text chat before the WebSocket connects', async () => {
    render(App)

    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: '送信' }).hasAttribute('disabled')).toBe(true)
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
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
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
    await waitFor(() => expect(mocks.recorderClose).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )
  })

  test('should disable text input and microphone capture while a text response is pending', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.input(screen.getByRole('textbox'), { target: { value: '少し待って' } })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.sent).toEqual([JSON.stringify({ type: 'text', message: '少し待って' })])
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )

    const disabledMicrophoneButton = screen.getByRole('button', { name: 'マイクをオンにする' })
    disabledMicrophoneButton.click()

    expect(mocks.getUserMedia).not.toHaveBeenCalled()
    expect(mocks.vadStart).not.toHaveBeenCalled()

    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', response: 'お待たせしました。' }),
      }),
    )

    expect(await screen.findByText('お待たせしました。')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      false,
    )
  })

  test('should keep text input disabled when the WebSocket closes', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.input(screen.getByRole('textbox'), { target: { value: '閉じても待って' } })
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

    await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'テキストもお願い' } })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.sent).toEqual([mocks.pcmData])
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(true)
    await waitFor(() => expect(mocks.recorderClose).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )

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
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )

    socket.onmessage?.(new MessageEvent('message', { data: new ArrayBuffer(12) }))

    await fireEvent.input(screen.getByRole('textbox'), { target: { value: 'テキストもお願い' } })
    await fireEvent.click(screen.getByRole('button', { name: '送信' }))

    expect(socket.sent).toEqual([
      mocks.pcmData,
      JSON.stringify({ type: 'text', message: 'テキストもお願い' }),
    ])
    socket.onmessage?.(
      new MessageEvent('message', {
        data: JSON.stringify({ type: 'text', response: 'テキストへの応答です。' }),
      }),
    )

    expect(await screen.findByText('テキストへの応答です。')).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'メッセージ' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      false,
    )
  })

  test('should disable microphone capture while the first audio response is pending', async () => {
    render(App)
    const socket = await openSocket()

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(mocks.vadStart).toHaveBeenCalledTimes(1))

    if (mocks.vadOptions === undefined) {
      throw new Error('VAD callbacks are required')
    }
    mocks.vadOptions.onSpeechEnd()
    await waitFor(() => expect(socket.sent).toEqual([mocks.pcmData]))

    await waitFor(() => expect(mocks.recorderClose).toHaveBeenCalledTimes(1))
    expect(mocks.vadDestroy).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )
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

    await waitFor(() => expect(mocks.recorderClose).toHaveBeenCalledTimes(1))
    expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
      true,
    )

    socket.onmessage?.(new MessageEvent('message', { data: new ArrayBuffer(12) }))

    expect(socket.sent).toEqual([mocks.pcmData])
    expect(screen.queryByText('応答の取得に失敗しました。')).toBeNull()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'マイクをオンにする' }).hasAttribute('disabled')).toBe(
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
