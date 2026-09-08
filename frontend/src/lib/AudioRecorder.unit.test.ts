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

let vadFrameClock = 10_000
const feedVadFrames = (
  callback: (probabilities: { isSpeech: number; notSpeech: number }, frame: Float32Array) => void,
  count: number, amplitude: number, probability: number,
) => {
  const clock = vi.spyOn(performance, 'now')
  try {
    for (let index = 0; index < count; index += 1) {
      vadFrameClock += 96
      clock.mockReturnValue(vadFrameClock)
      callback({ isSpeech: probability, notSpeech: 1 - probability }, new Float32Array(1536).fill(amplitude))
    }
  } finally { clock.mockRestore() }
}

describe('AudioRecorder', () => {
  beforeEach(() => {
    shortSpeechProcess.mockReset()
    shortSpeechProcess.mockReturnValue({voicedFraction: 0, tonalConcentration: 1, spectralFlatness: 1})
    shortSpeechReset.mockReset()
    shortSpeechClose.mockReset()
    vadFrameClock = 10_000
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

  test('再接続中はtrackを無音化して発話を破棄し、追加操作やマイク再取得なしで再開する', async () => {
    const started = vi.fn(), stopped = vi.fn()
    const {component} = render(AudioRecorder, {props: {disabled: false, forceOff: false, continuous: true,
      onSpeechStarted: started, onSpeechStopped: stopped, onAudioCaptured: createCaptureMock(), onError: vi.fn()}})
    const button = screen.getByRole('button', {name: 'マイクをオンにする'})
    await fireEvent.click(button)
    await waitFor(() => expect(button.getAttribute('aria-pressed')).toBe('true'))
    feedVadFrames(vadOptions.onFrameProcessed, 4, .01, .8)
    expect(started).toHaveBeenCalledTimes(1)
    await component.$set({suspended: true})
    expect(microphoneTrack.enabled).toBe(false)
    expect(button.getAttribute('aria-pressed')).toBe('true')
    expect(vadDestroy).not.toHaveBeenCalled()
    expect(microphoneTrackStop).not.toHaveBeenCalled()
    feedVadFrames(vadOptions.onFrameProcessed, 7, 0, .1)
    feedVadFrames(vadOptions.onFrameProcessed, 4, .01, .8)
    expect(started).toHaveBeenCalledTimes(1)
    expect(stopped).not.toHaveBeenCalled()
    await component.$set({suspended: false})
    expect(microphoneTrack.enabled).toBe(true)
    feedVadFrames(vadOptions.onFrameProcessed, 4, .01, .8)
    feedVadFrames(vadOptions.onFrameProcessed, 7, 0, .1)
    expect(started).toHaveBeenCalledTimes(2)
    expect(stopped).toHaveBeenCalledTimes(1)
    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(vadStart).toHaveBeenCalledTimes(1)
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

  test('継続modeではPCMを発話単位に停止せずVAD eventだけを通知する', async () => {
    const onSpeechStarted = vi.fn()
    const onSpeechStopped = vi.fn()
    const onMicrophoneEnabled = vi.fn(async () => undefined)
    render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
        continuous: true,
        onMicrophoneEnabled,
        onSpeechStarted,
        onSpeechStopped,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    feedVadFrames(vadOptions.onFrameProcessed, 4, 0.01, 0.8)
    feedVadFrames(vadOptions.onFrameProcessed, 7, 0, 0.8)
    // legacyの別callbackで開始・終了を二重通知しない。
    vadOptions.onSpeechStart()
    vadOptions.onSpeechRealStart()
    vadOptions.onSpeechEnd()

    await waitFor(() => expect(onSpeechStopped).toHaveBeenCalledTimes(1))
    expect(onMicrophoneEnabled).toHaveBeenCalledTimes(1)
    expect(onMicrophoneEnabled).toHaveBeenCalledWith(microphoneStream)
    expect(onSpeechStarted).toHaveBeenCalledWith({ clientMs: expect.any(Number) })
    expect(recorderInitialize).not.toHaveBeenCalled()
    expect(recorderStart).not.toHaveBeenCalled()
    expect(recorderStopAndTake).not.toHaveBeenCalled()
  })

  test('短い未確定発話は終了時に元の開始時刻を通知し、その直後に一度だけ終了する', async () => {
    const events: {type: string; clientMs: number}[] = []
    const onError = vi.fn()
    render(AudioRecorder, {props: {
      disabled: false, forceOff: false, continuous: true,
      onAudioCaptured: createCaptureMock(), onError,
      onSpeechStarted: activity => events.push({type: 'start', ...activity}),
      onSpeechStopped: activity => events.push({type: 'stop', ...activity}),
    }})
    await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    shortSpeechProcess.mockReturnValue({voicedFraction: 1, tonalConcentration: 0.4, spectralFlatness: 0.1})
    feedVadFrames(vadOptions.onFrameProcessed, 2, 0.01, 0.05)
    feedVadFrames(vadOptions.onFrameProcessed, 6, 0, 0.05)
    expect(events).toEqual([])
    feedVadFrames(vadOptions.onFrameProcessed, 1, 0, 0.05)
    expect(events).toEqual([{type: 'start', clientMs: 10_000}, {type: 'stop', clientMs: 10_864}])
    feedVadFrames(vadOptions.onFrameProcessed, 8, 0, 0.05)
    expect(events).toHaveLength(2)
    expect(onError).not.toHaveBeenCalled()
  })

  test('継続modeの停止で補助VADも解放し、再開時に作り直す', async () => {
    render(AudioRecorder, {props: {
      disabled: false, forceOff: false, continuous: true,
      onAudioCaptured: createCaptureMock(), onError: vi.fn(),
    }})
    await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    await fireEvent.click(screen.getByRole('button', {name: 'マイクをオフにする'}))
    await waitFor(() => expect(shortSpeechClose).toHaveBeenCalledTimes(1))
    expect(vadDestroy).toHaveBeenCalledTimes(1)
    expect(microphoneTrackStop).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(screen.getByRole('button', {name: 'マイクをオンにする'})).toBeTruthy())
    await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(2))
    expect(createShortSpeechAnalyzer).toHaveBeenCalledTimes(2)
  })

  test('静音で主VADを再開する前に補助VADをリセットし、保持したフレームを処理する', async () => {
    render(AudioRecorder, {props: {
      disabled: false, forceOff: false, continuous: true,
      onAudioCaptured: createCaptureMock(), onError: vi.fn(),
    }})
    await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))
    const instance = await vi.mocked(MicVAD.new).mock.results[0].value
    for (let i = 0; i < 8; i++) await instance.processFrame(new Float32Array(1536))
    expect(shortSpeechReset).toHaveBeenCalledTimes(1)
    expect(shortSpeechProcess).toHaveBeenCalledTimes(8)
    expect(shortSpeechReset.mock.invocationCallOrder[0]).toBeLessThan(vadStart.mock.invocationCallOrder[1])
    expect(vadStart.mock.invocationCallOrder[1]).toBeLessThan(shortSpeechProcess.mock.invocationCallOrder[7])
  })

  test('補助VADの初期化に失敗した場合もマイクを解放する', async () => {
    const error = new Error('Short speech asset failed')
    vi.mocked(createShortSpeechAnalyzer).mockRejectedValueOnce(error)
    const onError = vi.fn()
    render(AudioRecorder, {props: {
      disabled: false, forceOff: false, continuous: true,
      onAudioCaptured: createCaptureMock(), onError,
    }})
    await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
    await waitFor(() => expect(onError).toHaveBeenCalledWith(error))
    expect(microphoneTrackStop).toHaveBeenCalledTimes(1)
    expect(MicVAD.new).not.toHaveBeenCalled()
    expect(screen.getByRole('button', {name: 'マイクをオンにする'}).getAttribute('aria-pressed')).toBe('false')
  })

  test('主VADの初期化に失敗した場合は生成済みの補助VADを解放する', async () => {
    const error = new Error('Primary model failed')
    vi.mocked(MicVAD.new).mockRejectedValueOnce(error)
    const onError = vi.fn()
    render(AudioRecorder, {props: {
      disabled: false, forceOff: false, continuous: true,
      onAudioCaptured: createCaptureMock(), onError,
    }})
    await fireEvent.click(screen.getByRole('button', {name: 'マイクをオンにする'}))
    await waitFor(() => expect(onError).toHaveBeenCalledWith(error))
    expect(shortSpeechClose).toHaveBeenCalledTimes(1)
    expect(microphoneTrackStop).toHaveBeenCalledTimes(1)
  })

  test('VAD候補だけでは発話開始を通知せずmisfireを待機状態へ戻す', async () => {
    const onSpeechStarted = vi.fn()
    render(AudioRecorder, {
      props: {
        disabled: false,
        forceOff: false,
        continuous: true,
        onSpeechStarted,
        onAudioCaptured: createCaptureMock(),
        onError: vi.fn(),
      },
    })

    const button = screen.getByRole('button', { name: 'マイクをオンにする' })
    await fireEvent.click(button)
    await waitFor(() => expect(vadStart).toHaveBeenCalledTimes(1))

    feedVadFrames(vadOptions.onFrameProcessed, 1, 0.01, 0.1)
    expect(onSpeechStarted).not.toHaveBeenCalled()
    await waitFor(() => expect(button.classList.contains('mic-standby')).toBe(true))

    feedVadFrames(vadOptions.onFrameProcessed, 7, 0, 0.1)
    await waitFor(() => expect(button.classList.contains('mic-standby')).toBe(true))
    expect(onSpeechStarted).not.toHaveBeenCalled()
  })

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
