<script lang="ts">
  import { onDestroy } from 'svelte'
  import { MicVAD, type RealTimeVADOptions } from '@ricky0123/vad-web'

  import { AudioWorkletPcmRecorder } from './audio/pcm-worklet-recorder'
  import { VAD_ASSET_ROUTE } from './audio/vad-assets'

  type MicStatus = 'off' | 'standby' | 'on'

  type MicVadInstance = {
    start: () => Promise<void>
    destroy: () => Promise<void>
  }

  export let disabled: boolean
  export let onAudioCaptured: (pcmData: ArrayBuffer) => void
  export let onError: (error: Error) => void

  let vad: MicVadInstance | null = null
  let recorder: AudioWorkletPcmRecorder | null = null
  let status: MicStatus = 'off'
  let isLoading = false

  const requestMicrophoneStream = (): Promise<MediaStream> => {
    return navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    })
  }

  const buildVadOptions = (stream: MediaStream): Partial<RealTimeVADOptions> => ({
    baseAssetPath: VAD_ASSET_ROUTE,
    onnxWASMBasePath: VAD_ASSET_ROUTE,
    startOnLoad: false,
    getStream: async () => stream,
    resumeStream: async () => stream,
    pauseStream: async () => undefined,
    onSpeechStart: () => {
      try {
        getRecorder().start()
        setStatus('on')
      } catch (error) {
        reportError(error)
      }
    },
    onSpeechEnd: () => {
      void handleSpeechEnd()
    },
  })

  const getRecorder = (): AudioWorkletPcmRecorder => {
    if (recorder === null) {
      throw new Error('PCM recorder is not initialized')
    }

    return recorder
  }

  const reportError = (error: unknown) => {
    if (!(error instanceof Error)) {
      throw error
    }

    onError(error)
  }

  const releaseMicrophoneResources = async () => {
    if (vad !== null) {
      await vad.destroy()
      vad = null
    }

    if (recorder !== null) {
      await recorder.close()
      recorder = null
    }
  }

  const setStatus = (nextStatus: MicStatus) => {
    status = nextStatus
  }

  const getVad = async (stream: MediaStream): Promise<MicVadInstance> => {
    if (vad !== null) {
      return vad
    }

    vad = await MicVAD.new(buildVadOptions(stream))
    return vad
  }

  const enableMicrophone = async () => {
    isLoading = true
    try {
      if (recorder === null) {
        recorder = new AudioWorkletPcmRecorder()
      }

      const stream = await requestMicrophoneStream()
      await recorder.initialize(stream)
      const activeVad = await getVad(stream)
      await activeVad.start()
      setStatus('standby')
    } catch (error) {
      try {
        await releaseMicrophoneResources()
      } catch (cleanupError) {
        reportError(cleanupError)
      }
      setStatus('off')
      reportError(error)
    } finally {
      isLoading = false
    }
  }

  const disableMicrophone = async () => {
    await releaseMicrophoneResources()
    setStatus('off')
  }

  const handleSpeechEnd = async () => {
    try {
      const pcmData = await getRecorder().stopAndTake()
      onAudioCaptured(pcmData)

      setStatus('standby')
    } catch (error) {
      setStatus('standby')
      reportError(error)
    }
  }

  const toggleMicrophone = async () => {
    try {
      if (status === 'off') {
        await enableMicrophone()
        return
      }

      await disableMicrophone()
    } catch (error) {
      reportError(error)
    }
  }

  $: if (disabled && status !== 'off' && !isLoading) {
    setStatus('off')
    void releaseMicrophoneResources().catch(reportError)
  }
  $: buttonLabel = status === 'off' ? 'マイクをオンにする' : 'マイクをオフにする'
  $: isPressed = status !== 'off'
  $: isDisabled = isLoading || disabled

  onDestroy(() => {
    void vad?.destroy().catch(reportError)
    void recorder?.close().catch(reportError)
  })
</script>

<button
  type="button"
  class:mic-standby={status === 'standby'}
  class:mic-active={status === 'on'}
  disabled={isDisabled}
  aria-label={buttonLabel}
  aria-pressed={isPressed}
  on:click={toggleMicrophone}
>
  話す
</button>

<style>
  button {
    flex: 0 0 auto;
    min-width: 64px;
    min-height: 44px;
    padding: 0 14px;
    border: 1px solid rgba(144, 67, 47, 0.28);
    border-radius: 8px;
    color: #4a2822;
    background: #fffdfa;
    font-weight: 700;
    cursor: pointer;
  }

  button:disabled {
    cursor: wait;
    opacity: 0.58;
  }

  .mic-standby {
    border-color: #d88a2d;
    box-shadow: 0 0 0 3px rgba(216, 138, 45, 0.18);
  }

  .mic-active {
    color: #fffaf6;
    border-color: #c7352d;
    background: #c7352d;
    animation: pulse 1s ease-in-out infinite;
  }

  @keyframes pulse {
    0%,
    100% {
      box-shadow: 0 0 0 0 rgba(199, 53, 45, 0.32);
    }

    50% {
      box-shadow: 0 0 0 6px rgba(199, 53, 45, 0);
    }
  }
</style>
