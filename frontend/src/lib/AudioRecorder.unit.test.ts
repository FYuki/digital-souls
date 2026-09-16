import { fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import { MicVAD } from '@ricky0123/vad-web'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import AudioRecorder from './AudioRecorder.svelte'
import { createShortSpeechAnalyzer } from './audio/short-speech-evidence'
import { VAD_ASSET_ROUTE } from './audio/vad-assets'
import { VAD_UTTERANCE_REDEMPTION_MS } from './audio/vad-policy'

const shortSpeechProcess = vi.fn()
const shortSpeechReset = vi.fn()
const shortSpeechClose = vi.fn()
const vadStart = vi.fn()
const vadDestroy = vi.fn()
const recorderInitialize = vi.fn()
const recorderStart = vi.fn()
const recorderStopAndTake = vi.fn()
const recorderClose = vi.fn()
const capturedPcmData = new ArrayBuffer(4)
const getUserMedia = vi.fn()
const microphoneTrackStop = vi.fn()
const microphoneTrack = {enabled: true, stop: microphoneTrackStop}
const microphoneStream = {
  getTracks: () => [microphoneTrack],
  getAudioTracks: () => [microphoneTrack],
} as unknown as MediaStream
let vadOptions: {
  baseAssetPath: string
  onnxWASMBasePath: string
  redemptionMs: number
  getStream: () => Promise<MediaStream>
  resumeStream: (stream: MediaStream) => Promise<MediaStream>
  pauseStream: (stream: MediaStream) => Promise<void>
  startOnLoad: boolean
  onFrameProcessed: (probabilities: { isSpeech: number; notSpeech: number }, frame: Float32Array) => void
  onSpeechStart: () => void
  onSpeechRealStart: () => void
  onVADMisfire: () => void
  onSpeechEnd: () => void
}

vi.mock('@ricky0123/vad-web', () => ({
  MicVAD: {
    new: vi.fn(async (options) => {
      vadOptions = options
      return {
        processFrame: vi.fn(async (frame: Float32Array) => options.onFrameProcessed({isSpeech: 0.01, notSpeech: 0.99}, frame)),
        pause: vi.fn(async () => undefined),
        start: vadStart,
        destroy: vadDestroy,
      }
    }),
  },
}))

vi.mock('./audio/short-speech-evidence', () => ({
  createShortSpeechAnalyzer: vi.fn(async () => ({
    process: shortSpeechProcess,
    reset: shortSpeechReset,
    close: shortSpeechClose,
  })),
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
  return vi.fn((_pcmData: ArrayBuffer, _metadata: object): void => undefined)
}

describe('AudioRecorder', () => {
  beforeEach(() => {
    shortSpeechProcess.mockReset()
    shortSpeechProcess.mockReturnValue({voicedFraction: 0, tonalConcentration: 1, spectralFlatness: 1})
    shortSpeechReset.mockReset()
    shortSpeechClose.mockReset()
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
    microphoneTrackStop.mockReset()
    microphoneTrack.enabled = true
    vi.stubGlobal('navigator', {
      mediaDevices: {
        getUserMedia,
      },
    })
    vi.clearAllMocks()
  })

  test('再接続中はtrackを無音化し、復旧だけでは入力開始ACKの前に有効化しない', async () => {
    const enabled = vi.fn(async () => {microphoneTrack.enabled = true})
    const {component} = render(AudioRecorder, {props: {
      disabled: false, forceOff: false, continuous: true, onMicrophoneEnabled: enabled,
      onAudioCaptured: createCaptureMock(), onError: vi.fn(),
    }})
    const button = screen.getByRole('button', {name: 'マイクをオンにする'})
    await fireEvent.click(button)
    await waitFor(() => expect(button.getAttribute('aria-pressed')).toBe('true'))
    expect(microphoneTrack.enabled).toBe(true)
    await component.$set({suspended: true})
    expect(microphoneTrack.enabled).toBe(false)
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expect(microphoneTrackStop).not.toHaveBeenCalled()
    await component.$set({suspended: false})
    expect(microphoneTrack.enabled).toBe(false)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(MicVAD.new).not.toHaveBeenCalled()
  })

  test('再接続中に利用者がマイクをオフにした場合は復旧後も再取得しない', async () => {
    const disabled = vi.fn()
    const {component} = render(AudioRecorder, {props: {disabled: false, forceOff: false, continuous: true,
      onMicrophoneDisabled: disabled, onAudioCaptured: createCaptureMock(), onError: vi.fn()}})
    const button = screen.getByRole('button', {name: 'マイクをオンにする'})
    await fireEvent.click(button)
    await waitFor(() => expect(button.getAttribute('aria-pressed')).toBe('true'))
    await component.$set({suspended: true})
    expect((button as HTMLButtonElement).disabled).toBe(false)
    await fireEvent.click(button)
    await waitFor(() => expect(button.getAttribute('aria-pressed')).toBe('false'))
    expect(disabled).toHaveBeenCalledTimes(1)
    expect(microphoneTrackStop).toHaveBeenCalledTimes(1)
    expect((button as HTMLButtonElement).disabled).toBe(true)
    await component.$set({suspended: false})
    expect((button as HTMLButtonElement).disabled).toBe(false)
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  test('should expose an accessible inactive microphone button', () => {
    render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
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
        forceOff: false,
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
    expect(vadOptions.redemptionMs).toBe(VAD_UTTERANCE_REDEMPTION_MS)
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
        forceOff: false,
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

  // 発話境界・短発話・quiet resetの移設前後の同値性はBackendの実VAD parity試験が検証する。
  test('継続modeはVADと発話用recorderをロードせず、同じstreamをclientへ渡す', async () => {
    const enabled = vi.fn(async () => undefined)
    const started = vi.fn(), stopped = vi.fn()
    render(AudioRecorder, {props: {
      disabled: false, forceOff: false, continuous: true,
      onMicrophoneEnabled: enabled, onSpeechStarted: started, onSpeechStopped: stopped,
      onAudioCaptured: createCaptureMock(), onError: vi.fn(),
    }})
    await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
    await screen.findByRole('button', {name: 'マイクをオフにする'})
    expect(enabled).toHaveBeenCalledWith(microphoneStream)
    expect(microphoneTrack.enabled).toBe(false)
    expect(MicVAD.new).not.toHaveBeenCalled()
    expect(createShortSpeechAnalyzer).not.toHaveBeenCalled()
    expect(recorderInitialize).not.toHaveBeenCalled()
    expect(started).not.toHaveBeenCalled()
    expect(stopped).not.toHaveBeenCalled()
  })

  test('継続modeを停止して再開すると新しく取得したstreamをclientへ渡す', async () => {
    const enabled = vi.fn(async () => undefined)
    render(AudioRecorder, {props: {
      disabled: false, forceOff: false, continuous: true, onMicrophoneEnabled: enabled,
      onAudioCaptured: createCaptureMock(), onError: vi.fn(),
    }})
    await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
    await fireEvent.click(await screen.findByRole('button', {name: 'マイクをオフにする'}))
    await waitFor(() => expect(microphoneTrackStop).toHaveBeenCalledTimes(1))
    await fireEvent.click(await screen.findByRole('button', {name: 'マイクをオンにする'}))
    await screen.findByRole('button', {name: 'マイクをオフにする'})
    expect(getUserMedia).toHaveBeenCalledTimes(2)
    expect(enabled).toHaveBeenCalledTimes(2)
    expect(MicVAD.new).not.toHaveBeenCalled()
  })

  test.each(['publish failure', 'audio_input_open_timeout'])(
    '継続modeのclient開始失敗でtrackを解放しOFFへ戻す: %s', async message => {
      const error = new Error(message)
      const onError = vi.fn()
      render(AudioRecorder, {props: {
        disabled: false, forceOff: false, continuous: true,
        onMicrophoneEnabled: vi.fn(async () => {throw error}),
        onAudioCaptured: createCaptureMock(), onError,
      }})
      await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
      await waitFor(() => expect(onError).toHaveBeenCalledWith(error))
      expect(microphoneTrackStop).toHaveBeenCalledTimes(1)
      expect(microphoneTrack.enabled).toBe(false)
      expect(screen.getByRole('button', {name: 'マイクをオンにする'}).getAttribute('aria-pressed')).toBe('false')
      expect(MicVAD.new).not.toHaveBeenCalled()
    },
  )

  test('should publish ON status while speech is active', async () => {
    render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
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
        forceOff: false,
        onAudioCaptured,
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    vadOptions.onSpeechStart()
    vadOptions.onSpeechRealStart()
    vadOptions.onSpeechEnd()

    await waitFor(() => expect(onAudioCaptured).toHaveBeenCalledWith(
      capturedPcmData,
      {
        capturedAudioStartClientMs: expect.any(Number),
        vadSpeechEndClientMs: expect.any(Number),
        utteranceFinalizedClientMs: expect.any(Number),
        requiredManualOperations: 0,
      },
    ))
    expect(recorderStopAndTake).toHaveBeenCalledTimes(1)
    expect(recorderClose).not.toHaveBeenCalled()
    expect(button.classList.contains('mic-standby')).toBe(true)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should release VAD and publish OFF when the microphone is disabled from standby', async () => {
    render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
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
    expect(microphoneTrackStop).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(button.getAttribute('aria-pressed')).toBe('false'))
    expect(button.classList.contains('mic-standby')).toBe(false)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should release VAD and publish OFF when the microphone is disabled from active speech', async () => {
    render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
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
        forceOff: false,
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
        forceOff: false,
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
        forceOff: false,
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

  test('should keep standby resources while disabled without force off', async () => {
    const { component } = render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    await component.$set({ disabled: true, forceOff: false })

    await waitFor(() => expect(button.hasAttribute('disabled')).toBe(true))
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expect(button.classList.contains('mic-standby')).toBe(true)
    expect(button.classList.contains('mic-active')).toBe(false)
    expect(vadDestroy).not.toHaveBeenCalled()
    expect(recorderClose).not.toHaveBeenCalled()
  })

  test('should force microphone resources off when forceOff becomes true', async () => {
    const { component } = render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    await component.$set({ disabled: true, forceOff: true })

    await waitFor(() => expect(vadDestroy).toHaveBeenCalledTimes(1))
    expect(recorderClose).toHaveBeenCalledTimes(1)
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(button.classList.contains('mic-standby')).toBe(false)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should return to OFF and report error when getUserMedia is denied', async () => {
    const error = new Error('Permission denied')
    const onError = vi.fn()
    getUserMedia.mockRejectedValueOnce(error)

    render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
        onAudioCaptured: createCaptureMock(),
        onError,
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)

    await waitFor(() => expect(onError).toHaveBeenCalledWith(error))
    expect(recorderInitialize).not.toHaveBeenCalled()
    expect(vadDestroy).not.toHaveBeenCalled()
    expect(recorderClose).toHaveBeenCalledTimes(1)
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(button.classList.contains('mic-standby')).toBe(false)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should return to OFF and report error when recorder initialization fails', async () => {
    const error = new Error('AudioWorklet failed to load')
    const onError = vi.fn()
    recorderInitialize.mockRejectedValueOnce(error)

    render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
        onAudioCaptured: createCaptureMock(),
        onError,
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)

    await waitFor(() => expect(onError).toHaveBeenCalledWith(error))
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(vadDestroy).not.toHaveBeenCalled()
    expect(recorderClose).toHaveBeenCalledTimes(1)
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(button.classList.contains('mic-standby')).toBe(false)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should return to standby and report error when stopAndTake fails', async () => {
    const error = new Error('PCM concatenation failed')
    const onAudioCaptured = createCaptureMock()
    const onError = vi.fn()
    recorderStopAndTake.mockRejectedValueOnce(error)

    render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
        onAudioCaptured,
        onError,
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    vadOptions.onSpeechStart()
    vadOptions.onSpeechRealStart()
    vadOptions.onSpeechEnd()

    await waitFor(() => expect(onError).toHaveBeenCalledWith(error))
    expect(onAudioCaptured).not.toHaveBeenCalled()
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expect(button.classList.contains('mic-standby')).toBe(true)
    expect(button.classList.contains('mic-active')).toBe(false)
  })

  test('should keep OFF status and disable the button when initially forced off', () => {
    render(AudioRecorder, {
      props: {
        disabled: true,
        forceOff: true,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })

    expect(button.hasAttribute('disabled')).toBe(true)
    expect(button.getAttribute('aria-pressed')).toBe('false')
    expect(getUserMedia).not.toHaveBeenCalled()
    expect(vadStart).not.toHaveBeenCalled()
    expect(recorderClose).not.toHaveBeenCalled()
  })
})
