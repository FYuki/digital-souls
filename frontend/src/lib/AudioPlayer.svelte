<script lang="ts">
  import { onDestroy } from 'svelte'

  export let request: {
    audioData: ArrayBuffer
    onFirstPlayback: () => void
  } | null
  export let onError: (error: Error) => void

  let audioContext: AudioContext | null = null

  const getAudioContext = (): AudioContext => {
    if (audioContext !== null) {
      return audioContext
    }

    audioContext = new AudioContext()
    return audioContext
  }

  const playAudio = async (playbackRequest: NonNullable<typeof request>) => {
    try {
      const context = getAudioContext()
      const audioBuffer = await context.decodeAudioData(playbackRequest.audioData.slice(0))
      const source = context.createBufferSource()

      source.buffer = audioBuffer
      source.connect(context.destination)
      source.start()
      playbackRequest.onFirstPlayback()
    } catch (error) {
      if (!(error instanceof Error)) {
        throw error
      }

      onError(error)
    }
  }

  $: if (request !== null) {
    void playAudio(request)
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
