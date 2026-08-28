import { FrameProcessor, Message } from '@ricky0123/vad-web'
import { describe, expect, test } from 'vitest'

import { VAD_UTTERANCE_REDEMPTION_MS } from './vad-policy'

describe('継続音声入力のVAD発話境界', () => {
  test('1.3秒の短い間では分割せず、後続発話後の確定無音で一度だけ終端する', async () => {
    const probabilities: number[] = []
    const events: Message[] = []
    const frameMs = 100
    const processor = new FrameProcessor(
      async () => {
        const isSpeech = probabilities.shift()
        if (isSpeech === undefined) throw new Error('VAD fixture frame is missing')
        return { isSpeech, notSpeech: 1 - isSpeech }
      },
      () => undefined,
      {
        positiveSpeechThreshold: 0.3,
        negativeSpeechThreshold: 0.25,
        redemptionMs: VAD_UTTERANCE_REDEMPTION_MS,
        preSpeechPadMs: 0,
        minSpeechMs: 100,
        submitUserSpeechOnPause: false,
      },
      frameMs,
    )
    processor.resume()

    const feed = async (isSpeech: number, count: number) => {
      probabilities.push(...Array<number>(count).fill(isSpeech))
      for (let index = 0; index < count; index += 1) {
        await processor.process(new Float32Array([index]), (event) => {
          if (event.msg !== Message.FrameProcessed) events.push(event.msg)
        })
      }
    }

    await feed(0.9, 3)
    await feed(0.0, 13)
    expect(events).toEqual([Message.SpeechStart, Message.SpeechRealStart])

    await feed(0.9, 2)
    await feed(0.0, 14)

    expect(events).toEqual([
      Message.SpeechStart,
      Message.SpeechRealStart,
      Message.SpeechEnd,
    ])
  })
})
