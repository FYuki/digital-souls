import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import type { Page } from '@playwright/test'

// 合成発話だけを制御し、STT・LLM・TTS・LiveKitは実接続を維持する。
export async function prepareMicrophone(page: Page, initial: string) {
  const clips = Object.fromEntries(await Promise.all(['question', 'answer', 'slow', 'greeting'].map(async name => [name,
    (await readFile(join(process.env.TOOL_USE_TEST_AUDIO_DIR!, `${name}.wav`))).toString('base64'),
  ])))
  await page.addInitScript(({ clips, initial }) => {
    const native = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
    let context: AudioContext
    let destination: MediaStreamAudioDestinationNode
    const play = async (name: string) => {
      const bytes = Uint8Array.from(atob(clips[name]), c => c.charCodeAt(0))
      const buffer = await context.decodeAudioData(bytes.buffer)
      const source = context.createBufferSource()
      source.buffer = buffer
      source.connect(destination)
      source.start()
    }
    navigator.mediaDevices.getUserMedia = async constraints => {
      const granted = await native(constraints)
      granted.getTracks().forEach(track => track.stop())
      context = new AudioContext()
      destination = context.createMediaStreamDestination()
      destination.channelCount = 1
      await context.resume()
      setTimeout(() => { void play(initial) }, 2000)
      return destination.stream
    }
    ;(window as unknown as { __playToolVoice: typeof play }).__playToolVoice = play
  }, { clips, initial })
}

export async function playClip(page: Page, name: string) {
  await page.evaluate(name => (window as unknown as { __playToolVoice: (name: string) => Promise<void> }).__playToolVoice(name), name)
}
