import { fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import { MicVAD } from '@ricky0123/vad-web'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import AudioRecorder from './AudioRecorder.svelte'
import { VAD_ASSET_ROUTE } from './audio/vad-assets'

const vadStart = vi.fn()
const vadDestroy = vi.fn()
const recorderInitialize = vi.fn()
const recorderStart = vi.fn()
const recorderStopAndTake = vi.fn()
const recorderClose = vi.fn()
const capturedPcmData = new ArrayBuffer(4)
const getUserMedia = vi.fn()
const microphoneStream = {
  getTracks: () => [],
} as unknown as MediaStream
let vadOptions: {
  baseAssetPath: string
  onnxWASMBasePath: string
  getStream: () => Promise<MediaStream>
  resumeStream: (stream: MediaStream) => Promise<MediaStream>
  pauseStream: (stream: MediaStream) => Promise<void>
  startOnLoad: boolean
  onSpeechStart: () => void
  onSpeechEnd: () => void
}

vi.mock('@ricky0123/vad-web', () => ({
  MicVAD: {
    new: vi.fn(async (options) => {
      vadOptions = options
      return {
        start: vadStart,
        destroy: vadDestroy,
      }
    }),
  },
}))

vi.mock('./audio/pcm-worklet-recorder', () => ({
  AudioWorkletPcmRecorder: vi.fn(() => ({
    initialize: recorderInitialize,
    start: recorderStart,
    stopAndTake: recorderStopAndTake,
    close: recorderClose,
  })),
}))

const createCaptureMock = () => {
  return vi.fn((_pcmData: ArrayBuffer): void => undefined)
}

describe('AudioRecorder', () => {
  beforeEach(() => {
    vadStart.mockReset()
    vadStart.mockResolvedValue(undefined)
    vadDestroy.mockReset()
    vadDestroy.mockResolvedValue(undefined)
    recorderInitialize.mockReset()
    recorderInitialize.mockResolvedValue(undefined)
    recorderStart.mockReset()
    recorderStopAndTake.mockReset()
    recorderStopAndTake.mockResolvedValue(capturedPcmData)
    recorderClose.mockReset()
    recorderClose.mockResolvedValue(undefined)
    getUserMedia.mockReset()
    getUserMedia.mockResolvedValue(microphoneStream)
    vi.stubGlobal('navigator', {
      mediaDevices: {
        getUserMedia,
      },
    })
    vi.clearAllMocks()
  })

  test('should expose an accessible inactive microphone button', () => {
    render(AudioRecorder, {
      props: {
        disabled: false,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })

    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(button.classList.contains('mic-standby')).toBe(false)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should initialize VAD and enter standby when the microphone is enabled', async () => {
    render(AudioRecorder, {
      props: {
        disabled: false,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)

    await waitFor(() => expect(recorderInitialize).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    })
    expect(recorderInitialize).toHaveBeenCalledWith(microphoneStream)
    expect(vadOptions.baseAssetPath).toBe(VAD_ASSET_ROUTE)
    expect(vadOptions.onnxWASMBasePath).toBe(VAD_ASSET_ROUTE)
    expect(vadOptions.startOnLoad).toBe(false)
    await expect(vadOptions.getStream()).resolves.toBe(microphoneStream)
    await expect(vadOptions.resumeStream(microphoneStream)).resolves.toBe(microphoneStream)
    await expect(vadOptions.pauseStream(microphoneStream)).resolves.toBeUndefined()
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expect(button.classList.contains('mic-standby')).toBe(true)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should share one microphone stream between VAD and PCM recording', async () => {
    render(AudioRecorder, {
      props: {
        disabled: false,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))

    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(recorderInitialize).toHaveBeenCalledWith(microphoneStream)
    await expect(vadOptions.getStream()).resolves.toBe(microphoneStream)
  })

  test('should publish ON status while speech is active', async () => {
    render(AudioRecorder, {
      props: {
        disabled: false,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    vadOptions.onSpeechStart()

    expect(recorderStart).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(button.classList.contains('mic-active')).toBe(true))
    expect(button.classList.contains('mic-standby')).toBe(false)
  })

  test('should publish AudioWorklet PCM and return to standby after speech ends', async () => {
    const onAudioCaptured = createCaptureMock()
    render(AudioRecorder, {
      props: {
        disabled: false,
        onAudioCaptured,
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    vadOptions.onSpeechEnd()

    await waitFor(() => expect(onAudioCaptured).toHaveBeenCalledWith(capturedPcmData))
    expect(recorderStopAndTake).toHaveBeenCalledTimes(1)
    expect(recorderClose).not.toHaveBeenCalled()
    expect(button.classList.contains('mic-standby')).toBe(true)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should release VAD and publish OFF when the microphone is disabled from standby', async () => {
    render(AudioRecorder, {
      props: {
        disabled: false,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    await fireEvent.click(button)

    await waitFor(() => expect(vadDestroy).toHaveBeenCalledTimes(1))
    expect(recorderClose).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(button.getAttribute('aria-pressed')).toBe('false'))
    expect(button.classList.contains('mic-standby')).toBe(false)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should release VAD and publish OFF when the microphone is disabled from active speech', async () => {
    render(AudioRecorder, {
      props: {
        disabled: false,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    vadOptions.onSpeechStart()
    await fireEvent.click(button)

    await waitFor(() => expect(vadDestroy).toHaveBeenCalledTimes(1))
    expect(recorderClose).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(button.getAttribute('aria-pressed')).toBe('false'))
    expect(button.classList.contains('mic-standby')).toBe(false)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should destroy VAD when the recorder component is torn down', async () => {
    const { unmount } = render(AudioRecorder, {
      props: {
        disabled: false,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    unmount()

    expect(vadDestroy).toHaveBeenCalledTimes(1)
  })

  test('should report microphone initialization failures and return to OFF status', async () => {
    const error = new Error('VAD assets failed to load')
    const onError = vi.fn()
    vadStart.mockRejectedValueOnce(error)

    render(AudioRecorder, {
      props: {
        disabled: false,
        onAudioCaptured: createCaptureMock(),
        onError,
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)

    await waitFor(() => expect(onError).toHaveBeenCalledWith(error))
    expect(vadDestroy).toHaveBeenCalledTimes(1)
    expect(recorderClose).toHaveBeenCalledTimes(1)
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(button.classList.contains('mic-standby')).toBe(false)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should release the recorder when VAD creation fails', async () => {
    const error = new Error('VAD model failed to load')
    const onError = vi.fn()
    vi.mocked(MicVAD.new).mockRejectedValueOnce(error)

    render(AudioRecorder, {
      props: {
        disabled: false,
        onAudioCaptured: createCaptureMock(),
        onError,
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)

    await waitFor(() => expect(onError).toHaveBeenCalledWith(error))
    expect(vadDestroy).not.toHaveBeenCalled()
    expect(recorderClose).toHaveBeenCalledTimes(1)
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(button.classList.contains('mic-standby')).toBe(false)
    expect(button.classList.contains('mic-active')).toBe(false)
  })
})
