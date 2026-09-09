import { describe, expect, test } from 'vitest'
import { outputFrameTimeMs, renderWorkletSource } from './livekit/render-worklet'
import { PlaybackEvidenceController, type PlaybackEvidence } from './livekit/playback'

const processor = (frame: number) => {
  const messages: Record<string, unknown>[] = []
  class Base {
    port = { postMessage: (message: Record<string, unknown>) => messages.push(message) }
  }
  let create: (() => { process: (inputs: Float32Array[][], outputs: Float32Array[][]) => boolean }) | undefined
  new Function('AudioWorkletProcessor', 'registerProcessor', 'currentFrame', renderWorkletSource)(
    Base,
    (_name: string, Constructor: new () => ReturnType<NonNullable<typeof create>>) => {
      create = () => new Constructor()
    },
    frame,
  )
  if (create === undefined) throw new Error('processor was not registered')
  const instance = create()
  return { process: instance.process.bind(instance), messages }
}

describe('実出力sampleの観測', () => {
  test('入力を音声出力へコピーし、最初の非無音sampleのframeだけを通知する', () => {
    const { process, messages } = processor(48000)
    const input = new Float32Array([0, 0, 0.5, -0.5])
    const output = new Float32Array(4)
    expect(process([[input]], [[output]])).toBe(true)
    expect(output).toEqual(input)
    expect(messages).toEqual([{
      startFrame: 48000, endFrame: 48004, energy: 0.5, firstAudibleFrame: 48002,
    }])
    expect(JSON.stringify(messages)).not.toContain('0.5,-0.5')
  })

  test('入力のないquantumはゼロ出力であり、再生開始を捏造しない', () => {
    const { process, messages } = processor(48000)
    const output = new Float32Array([1, 1, 1, 1])
    process([[]], [[output]])
    expect(output).toEqual(new Float32Array(4))
    expect(messages).toEqual([{ startFrame: 48000, endFrame: 48004, energy: 0 }])
  })

  test('遅れて到着した通知でもcallback到着時刻ではなくframeの出力時刻へ変換する', () => {
    expect(outputFrameTimeMs(48000, 48000, {
      contextTime: 2, performanceTime: 3500,
    })).toBe(2500)
    expect(outputFrameTimeMs(48000, 48000, {
      contextTime: 3, performanceTime: 4500,
    })).toBe(2500)
    expect(outputFrameTimeMs(48000, 48000, {
      contextTime: 0, performanceTime: 0,
    })).toBeUndefined()
  })

  test('前responseのenergyを次responseの再生開始として使わない', () => {
    const observed: PlaybackEvidence[] = []
    const controller = new PlaybackEvidenceController(0, (value) => observed.push(value))
    controller.recordMetadata({ responseId: 'old', audioSequence: 0, generation: 0, pcmSampleCount: 4 }, 0)
    controller.recordRenderedInterval({ startFrame: 0, endFrame: 4, energy: 1, firstAudibleFrame: 2 })
    expect(observed.at(-1)?.firstPlaybackFrame).toBe(2)
    controller.recordMetadata({ responseId: 'new', audioSequence: 0, generation: 0, pcmSampleCount: 8 }, 4)
    controller.recordRenderedInterval({ startFrame: 4, endFrame: 8, energy: 0 })
    expect(observed.at(-1)?.firstPlaybackFrame).toBeUndefined()
    controller.recordRenderedInterval({ startFrame: 8, endFrame: 12, energy: 1, firstAudibleFrame: 9 })
    expect(observed.at(-1)?.firstPlaybackFrame).toBe(9)
  })
})
