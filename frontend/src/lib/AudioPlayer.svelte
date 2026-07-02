<script lang="ts">
  import { onDestroy } from 'svelte'

  export let audioData: ArrayBuffer | null
  export let onError: (error: Error) => void

  let audioContext: AudioContext | null = null

  const getAudioContext = (): AudioContext => {
    if (audioContext !== null) {
      return audioContext
    }

    audioContext = new AudioContext()
    return audioContext
  }

  const playAudio = async (data: ArrayBuffer) => {
    try {
      const context = getAudioContext()
      const audioBuffer = await context.decodeAudioData(data.slice(0))
      const source = context.createBufferSource()

      source.buffer = audioBuffer
      source.connect(context.destination)
      source.start()
    } catch (error) {
      if (!(error instanceof Error)) {
        throw error
      }

      onError(error)
    }
  }

  $: if (audioData !== null) {
    void playAudio(audioData)
  }

  onDestroy(() => {
    void audioContext?.close().catch((error: unknown) => {
      if (!(error instanceof Error)) {
        throw error
      }

      onError(error)
    })
  })
</script>
