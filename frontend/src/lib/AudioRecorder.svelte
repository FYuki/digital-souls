<script context="module" lang="ts">
  export type AudioCaptureMetadata = {
    capturedAudioStartClientMs: number
    vadSpeechEndClientMs: number
    utteranceFinalizedClientMs: number
    requiredManualOperations: number
  }

  export type SpeechActivity = {
    clientMs: number
  }
</script>

<script lang="ts">
  import { onDestroy } from 'svelte'
  import { MicVAD, type RealTimeVADOptions } from '@ricky0123/vad-web'

  import { AudioWorkletPcmRecorder } from './audio/pcm-worklet-recorder'
  import { VAD_ASSET_ROUTE } from './audio/vad-assets'
  import { VAD_UTTERANCE_REDEMPTION_MS } from './audio/vad-policy'

  type MicStatus = 'off' | 'standby' | 'on'

  type MicVadInstance = {
    start: () => Promise<void>
    destroy: () => Promise<void>
  }

  export let disabled: boolean
  export let forceOff: boolean
  export let onAudioCaptured: (
    pcmData: ArrayBuffer,
    metadata: AudioCaptureMetadata,
  ) => void
  export let onError: (error: Error) => void
  export let continuous = false
  export let onBeforeEnable: () => Promise<void> = async () => undefined
  export let onMicrophoneEnabled: () => Promise<void> = async () => undefined
  export let onMicrophoneDisabled: () => Promise<void> = async () => undefined
  export let onSpeechStarted: (activity: SpeechActivity) => void = () => undefined
  export let onSpeechStopped: (activity: SpeechActivity) => void = () => undefined

  let vad: MicVadInstance | null = null
  let recorder: AudioWorkletPcmRecorder | null = null
  let microphoneStream: MediaStream | null = null
  let status: MicStatus = 'off'
  let isLoading = false
  let capturedAudioStartClientMs: number | null = null

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
    redemptionMs: VAD_UTTERANCE_REDEMPTION_MS,
    startOnLoad: false,
    getStream: async () => stream,
    resumeStream: async () => stream,
    pauseStream: async () => undefined,
    onSpeechStart: () => {
      try {
        capturedAudioStartClientMs = performance.now()
        onSpeechStarted({ clientMs: capturedAudioStartClientMs })
        if (!continuous) getRecorder().start()
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
    for (const track of microphoneStream?.getTracks() ?? []) track.stop()
    microphoneStream = null
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
      await onBeforeEnable()
      if (!continuous && recorder === null) {
        recorder = new AudioWorkletPcmRecorder()
      }

      const stream = await requestMicrophoneStream()
      microphoneStream = stream
      if (!continuous) await getRecorder().initialize(stream)
      const activeVad = await getVad(stream)
      await activeVad.start()
      await onMicrophoneEnabled()
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
    try {
      await onMicrophoneDisabled()
    } finally {
      await releaseMicrophoneResources()
      setStatus('off')
    }
  }

  const handleSpeechEnd = async () => {
    const vadSpeechEndClientMs = performance.now()
    try {
      if (capturedAudioStartClientMs === null) {
        throw new Error('Speech start timestamp is not available')
      }
      onSpeechStopped({ clientMs: vadSpeechEndClientMs })
      if (continuous) {
        capturedAudioStartClientMs = null
        setStatus('standby')
        return
      }
      const pcmData = await getRecorder().stopAndTake()
      const utteranceFinalizedClientMs = performance.now()
      onAudioCaptured(pcmData, {
        capturedAudioStartClientMs,
        vadSpeechEndClientMs,
        utteranceFinalizedClientMs,
        requiredManualOperations: 0,
      })
      capturedAudioStartClientMs = null

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

  $: if (forceOff && status !== 'off' && !isLoading) {
    setStatus('off')
    void releaseMicrophoneResources().catch(reportError)
  }
  $: buttonLabel = status === 'off' ? 'マイクをオンにする' : 'マイクをオフにする'
  $: isPressed = status !== 'off'
  $: isDisabled = isLoading || disabled

  onDestroy(() => {
    void releaseMicrophoneResources().catch(reportError)
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
