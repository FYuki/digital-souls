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
  import type { RealTimeVADOptions } from '@ricky0123/vad-web'

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
  export let suspended = false
  export let onBeforeEnable: () => Promise<void> = async () => undefined
  export let onMicrophoneEnabled: (stream: MediaStream) => Promise<void> = async () => undefined
  export let onMicrophoneDisabled: () => Promise<void> = async () => undefined
  export let onSpeechStarted: (activity: SpeechActivity) => void = () => undefined
  export let onSpeechStopped: (activity: SpeechActivity) => void = () => undefined

  let vad: MicVadInstance | null = null
  let recorder: AudioWorkletPcmRecorder | null = null
  let microphoneStream: MediaStream | null = null
  let status: MicStatus = 'off'
  let isLoading = false
  let destroyed = false
  let activationGeneration = 0
  let observedForceOff = forceOff

  const isCurrentActivation = (generation: number) => !destroyed && generation === activationGeneration
  let candidateSpeechStartClientMs: number | null = null
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

  const buildVadOptions = (stream: MediaStream, generation: number): Partial<RealTimeVADOptions> => ({
    model: 'legacy',
    baseAssetPath: VAD_ASSET_ROUTE,
    onnxWASMBasePath: VAD_ASSET_ROUTE,
    redemptionMs: VAD_UTTERANCE_REDEMPTION_MS,
    startOnLoad: false,
    getStream: async () => stream,
    resumeStream: async () => stream,
    pauseStream: async () => undefined,
    onSpeechStart: () => {
      if (continuous || !isCurrentActivation(generation)) return
      try {
        candidateSpeechStartClientMs = performance.now()
        if (!continuous) getRecorder().start()
        setStatus('on')
      } catch (error) {
        reportError(error)
      }
    },
    onSpeechRealStart: () => {
      if (continuous || !isCurrentActivation(generation)) return
      capturedAudioStartClientMs = candidateSpeechStartClientMs ?? performance.now()
      candidateSpeechStartClientMs = null
      onSpeechStarted({ clientMs: capturedAudioStartClientMs })
    },
    onVADMisfire: () => {
      if (continuous || !isCurrentActivation(generation)) return
      void handleVadMisfire()
    },
    onSpeechEnd: () => {
      if (continuous || !isCurrentActivation(generation)) return
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
    candidateSpeechStartClientMs = null
    capturedAudioStartClientMs = null
    // 古いcleanupが遅れて完了しても、後の資源を消さないよう先に所有を外す。
    const previousVad = vad
    const previousRecorder = recorder
    const previousStream = microphoneStream
    vad = null
    recorder = null
    microphoneStream = null
    // VAD/AudioContextの終了待ちより先にdeviceを停止する。
    for (const track of previousStream?.getTracks() ?? []) track.stop()
    try {
      if (previousVad !== null) await previousVad.destroy()
    } finally {
      if (previousRecorder !== null) await previousRecorder.close()
    }
  }

  const setStatus = (nextStatus: MicStatus) => {
    status = nextStatus
  }

  const getVad = async (stream: MediaStream, generation: number): Promise<MicVadInstance | null> => {
    if (vad !== null) {
      return vad
    }

    const {MicVAD} = await import('@ricky0123/vad-web')
    if (!isCurrentActivation(generation)) return null
    const instance = await MicVAD.new(buildVadOptions(stream, generation))
    if (!isCurrentActivation(generation)) {
      await instance.destroy()
      return null
    }
    vad = instance
    return vad
  }

  const enableMicrophone = async () => {
    const generation = ++activationGeneration
    isLoading = true
    try {
      await onBeforeEnable()
      if (!isCurrentActivation(generation)) return
      if (!continuous && recorder === null) {
        recorder = new AudioWorkletPcmRecorder()
      }

      const stream = await requestMicrophoneStream()
      if (!isCurrentActivation(generation)) {
        for (const track of stream.getTracks()) track.stop()
        return
      }
      microphoneStream = stream
      if (continuous) {
        for (const track of stream.getAudioTracks()) track.enabled = false
      } else {
        const activeRecorder = getRecorder()
        await activeRecorder.initialize(stream)
        if (!isCurrentActivation(generation)) {
          await activeRecorder.close()
          return
        }
        const activeVad = await getVad(stream, generation)
        if (activeVad === null) return
        await activeVad.start()
        if (!isCurrentActivation(generation)) {
          await activeVad.destroy()
          return
        }
      }
      if (!isCurrentActivation(generation)) return
      await onMicrophoneEnabled(stream)
      if (!isCurrentActivation(generation)) return
      setStatus('standby')
    } catch (error) {
      if (!isCurrentActivation(generation)) return
      try {
        await releaseMicrophoneResources()
      } catch (cleanupError) {
        reportError(cleanupError)
      }
      setStatus('off')
      reportError(error)
    } finally {
      // initialize等が取消後に資源を作った場合も、その開始操作の終了までに回収する。
      if (!isCurrentActivation(generation)) {
        try { await releaseMicrophoneResources() }
        catch (error) { if (!destroyed) reportError(error) }
      }
      isLoading = false
    }
  }

  const disableMicrophone = async () => {
    ++activationGeneration
    try {
      await onMicrophoneDisabled()
    } finally {
      await releaseMicrophoneResources()
      setStatus('off')
    }
  }

  const handleSpeechEnd = async (vadSpeechEndClientMs = performance.now()) => {
    const generation = activationGeneration
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
      if (!isCurrentActivation(generation)) return
      const utteranceFinalizedClientMs = performance.now()
      onAudioCaptured(pcmData, {
        capturedAudioStartClientMs,
        vadSpeechEndClientMs,
        utteranceFinalizedClientMs,
        requiredManualOperations: 0,
      })
      capturedAudioStartClientMs = null
      candidateSpeechStartClientMs = null

      setStatus('standby')
    } catch (error) {
      if (!isCurrentActivation(generation)) return
      setStatus('standby')
      reportError(error)
    }
  }

  const handleVadMisfire = async () => {
    const generation = activationGeneration
    candidateSpeechStartClientMs = null
    capturedAudioStartClientMs = null
    try {
      if (!continuous) await getRecorder().stopAndTake()
    } catch (error) {
      if (isCurrentActivation(generation)) reportError(error)
    } finally {
      if (isCurrentActivation(generation)) setStatus('standby')
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

  const suspendCapture = (paused: boolean, stream: MediaStream | null) => {
    // 再接続中は同じtrackを無音化し、利用者が選んだマイクのON/OFFを保持する。
    // 途中の発話は次の接続へ継ぎ足さず、復旧後に新しい発話境界を検出する。
    // continuousの再有効化はBEの入力認可ACK後にcontrollerが行う。
    if (paused) for (const track of stream?.getTracks() ?? []) track.enabled = false
    if (paused) {
      candidateSpeechStartClientMs = null
      capturedAudioStartClientMs = null
      if (status === 'on') setStatus('standby')
    }
  }

  const observeForcedOff = (next: boolean) => {
    const newlyForcedOff = next && !observedForceOff
    observedForceOff = next
    if (!newlyForcedOff) return
    ++activationGeneration
    setStatus('off')
    void releaseMicrophoneResources().catch(reportError)
  }

  $: observeForcedOff(forceOff)
  $: suspendCapture(continuous && suspended, microphoneStream)
  $: if (forceOff && status !== 'off' && !isLoading) {
    setStatus('off')
    void releaseMicrophoneResources().catch(reportError)
  }
  $: buttonLabel = status === 'off' ? 'マイクをオンにする' : 'マイクをオフにする'
  $: isPressed = status !== 'off'
  $: isDisabled = isLoading || disabled || (suspended && status === 'off')

  onDestroy(() => {
    destroyed = true
    ++activationGeneration
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
