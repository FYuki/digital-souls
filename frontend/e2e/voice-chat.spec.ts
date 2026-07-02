import { fileURLToPath } from 'node:url'

import { expect, test, type Page, type TestInfo } from '@playwright/test'

import {
  resolveVoiceChatBackend,
  type VoiceChatBackend,
} from './voice-chat-backend'
import { installMockWebSocketBackend } from './mock-web-socket'

declare global {
  interface Window {
    __voiceChatE2E: {
      cycles: {
        sendAt: number
        audioReceivedAt: number | null
        audioDecodeAt: number | null
        startedAt: number | null
      }[]
      frameOrder: string[]
    }
  }
}

const speechFixturePath = fileURLToPath(new URL('./fixtures/speech.wav', import.meta.url))
const VOICE_RESPONSE_TIMEOUT_MS = 60000
const VOICE_TEST_TIMEOUT_MS = 120000
let voiceChatBackend: VoiceChatBackend

type CompletedVoiceCycle = {
  sendAt: number
  audioReceivedAt: number
  audioDecodeAt: number
  startedAt: number
  latencyMs: number
}

const createMockAudioResponse = (): Buffer => {
  const sampleRate = 16000
  const durationSeconds = 0.2
  const samples = Math.floor(sampleRate * durationSeconds)
  const dataSize = samples * 2
  const buffer = Buffer.alloc(44 + dataSize)

  buffer.write('RIFF', 0)
  buffer.writeUInt32LE(36 + dataSize, 4)
  buffer.write('WAVE', 8)
  buffer.write('fmt ', 12)
  buffer.writeUInt32LE(16, 16)
  buffer.writeUInt16LE(1, 20)
  buffer.writeUInt16LE(1, 22)
  buffer.writeUInt32LE(sampleRate, 24)
  buffer.writeUInt32LE(sampleRate * 2, 28)
  buffer.writeUInt16LE(2, 32)
  buffer.writeUInt16LE(16, 34)
  buffer.write('data', 36)
  buffer.writeUInt32LE(dataSize, 40)

  for (let index = 0; index < samples; index += 1) {
    const sample = Math.round(Math.sin((2 * Math.PI * 440 * index) / sampleRate) * 12000)
    buffer.writeInt16LE(sample, 44 + index * 2)
  }

  return buffer
}

const mockAudioResponse = createMockAudioResponse()

test.use({
  launchOptions: {
    args: [
      '--use-fake-device-for-media-stream',
      '--use-fake-ui-for-media-stream',
      `--use-file-for-fake-audio-capture=${speechFixturePath}`,
    ],
  },
  permissions: ['microphone'],
})
test.describe.configure({ mode: 'serial' })
test.setTimeout(VOICE_TEST_TIMEOUT_MS)

test.beforeAll(async () => {
  voiceChatBackend = await resolveVoiceChatBackend()
})

const installPlaybackProbe = async (page: Page) => {
  await page.addInitScript(() => {
    window.__voiceChatE2E = {
      ...window.__voiceChatE2E,
      cycles: [],
      frameOrder: [],
    }

    const isAudioFrame = (data: unknown): data is ArrayBuffer | ArrayBufferView | Blob => {
      return data instanceof ArrayBuffer || ArrayBuffer.isView(data) || data instanceof Blob
    }

    const recordAudioSend = (data: unknown) => {
      if (!isAudioFrame(data)) {
        return
      }

      window.__voiceChatE2E.cycles.push({
        sendAt: performance.now(),
        audioReceivedAt: null,
        audioDecodeAt: null,
        startedAt: null,
      })
    }

    const recordAudioResponse = (data: unknown) => {
      if (!isAudioFrame(data)) {
        return
      }

      window.__voiceChatE2E.frameOrder.push('audio')
      const cycle = window.__voiceChatE2E.cycles.find((candidate) => candidate.audioReceivedAt === null)
      if (cycle !== undefined) {
        cycle.audioReceivedAt = performance.now()
      }
    }

    const recordTextResponse = (data: unknown) => {
      if (typeof data !== 'string') {
        return
      }

      const parsed = JSON.parse(data) as { type?: unknown; speaker?: unknown }
      if (parsed.type !== 'text') {
        return
      }

      if (parsed.speaker === 'user') {
        window.__voiceChatE2E.frameOrder.push('user-text')
        return
      }

      if (parsed.speaker === 'miori') {
        window.__voiceChatE2E.frameOrder.push('miori-text')
      }
    }

    const NativeWebSocket = WebSocket
    window.WebSocket = class WebSocketWithPlaybackProbe extends NativeWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        if (protocols === undefined) {
          super(url)
        } else {
          super(url, protocols)
        }

        this.addEventListener('message', (event) => {
          recordTextResponse(event.data)
          recordAudioResponse(event.data)
        })
      }

      send(data: string | ArrayBufferLike | Blob | ArrayBufferView) {
        recordAudioSend(data)
        super.send(data)
      }
    } as typeof WebSocket

    const originalDecodeAudioData = AudioContext.prototype.decodeAudioData
    AudioContext.prototype.decodeAudioData = function decodeAudioDataWithProbe(
      this: AudioContext,
      audioData: ArrayBuffer,
      successCallback?: DecodeSuccessCallback | null,
      errorCallback?: DecodeErrorCallback | null,
    ) {
      const cycle = window.__voiceChatE2E.cycles.find((candidate) => {
        return candidate.audioReceivedAt !== null && candidate.audioDecodeAt === null
      })
      if (cycle !== undefined) {
        cycle.audioDecodeAt = performance.now()
      }

      return originalDecodeAudioData.call(this, audioData, successCallback, errorCallback)
    }

    const originalCreateBufferSource = AudioContext.prototype.createBufferSource
    AudioContext.prototype.createBufferSource = function createBufferSourceWithProbe(this: AudioContext) {
      const source = originalCreateBufferSource.call(this)
      const originalStart = source.start.bind(source)
      source.start = (when?: number, offset?: number, duration?: number) => {
        const cycle = window.__voiceChatE2E.cycles.find((candidate) => {
          return candidate.audioReceivedAt !== null && candidate.audioDecodeAt !== null && candidate.startedAt === null
        })
        if (cycle !== undefined) {
          cycle.startedAt = performance.now()
        }
        originalStart(when, offset, duration)
      }
      return source
    }
  })
}

const attachBackendModeEvidence = async (testInfo: TestInfo) => {
  testInfo.annotations.push({
    type: 'voice-chat-backend',
    description: voiceChatBackend.mode,
  })
  await testInfo.attach('voice-chat-backend.json', {
    body: JSON.stringify(voiceChatBackend, null, 2),
    contentType: 'application/json',
  })
}

const installMockVoiceBackend = async (page: Page) => {
  if (voiceChatBackend.mode !== 'mock') {
    return
  }

  await installMockWebSocketBackend(page, {
    textFrames: [],
    binaryFrames: [
      { kind: 'text', data: JSON.stringify({ type: 'text', speaker: 'user', message: 'テスト音声です' }) },
      { kind: 'text', data: JSON.stringify({ type: 'text', speaker: 'miori', response: 'テスト音声に応答します。' }) },
      { kind: 'binary', data: Array.from(mockAudioResponse) },
    ],
  })
}

const installVoiceChatTestHarness = async (page: Page) => {
  await installMockVoiceBackend(page)
  await installPlaybackProbe(page)
}

const openVoiceChat = async (page: Page, testInfo: TestInfo) => {
  await attachBackendModeEvidence(testInfo)
  await installVoiceChatTestHarness(page)
  await page.goto('/')

  const micButton = page.getByRole('button', { name: /マイクを(オン|オフ)にする/ })
  await expect(micButton).toBeEnabled()

  return micButton
}

const enableMicrophone = async (page: Page, testInfo: TestInfo) => {
  const micButton = await openVoiceChat(page, testInfo)
  await micButton.click()
  await expect(micButton).toHaveAttribute('aria-pressed', 'true')

  return micButton
}

const expectVoiceChatMessages = async (page: Page) => {
  const messages = page.locator('article.message')
  await expect
    .poll(async () => messages.count(), { timeout: VOICE_RESPONSE_TIMEOUT_MS })
    .toBeGreaterThanOrEqual(2)
  await expect(messages.nth(0).locator('.speaker')).toHaveText('あなた')
  await expect(messages.nth(1).locator('.speaker')).toHaveText('光織')

  await expect(messages.nth(0).locator('p')).not.toHaveText('')
  await expect(messages.nth(1).locator('p')).not.toHaveText('')
}

const waitForSpeechDetection = async (page: Page) => {
  const micButton = page.getByRole('button', { name: /マイクを(オン|オフ)にする/ })
  await expect(micButton).toHaveClass(/mic-active/, { timeout: 15000 })

  return micButton
}

const waitForSpeechCompletion = async (page: Page) => {
  await waitForSpeechDetection(page)
  await page.waitForFunction(
    () => window.__voiceChatE2E.cycles.length > 0,
    undefined,
    { timeout: 15000 },
  )
}

const waitForCompletedVoiceCycle = async (page: Page): Promise<CompletedVoiceCycle> => {
  const cycleHandle = await page.waitForFunction(
    () => {
      const completedCycle = window.__voiceChatE2E.cycles.find((candidate) => {
        return (
          candidate.audioReceivedAt !== null &&
          candidate.audioDecodeAt !== null &&
          candidate.startedAt !== null
        )
      })
      if (
        completedCycle === undefined ||
        completedCycle.audioReceivedAt === null ||
        completedCycle.audioDecodeAt === null ||
        completedCycle.startedAt === null
      ) {
        return null
      }

      return {
        sendAt: completedCycle.sendAt,
        audioReceivedAt: completedCycle.audioReceivedAt,
        audioDecodeAt: completedCycle.audioDecodeAt,
        startedAt: completedCycle.startedAt,
        latencyMs: completedCycle.startedAt - completedCycle.sendAt,
      }
    },
    undefined,
    { timeout: VOICE_RESPONSE_TIMEOUT_MS },
  )
  const cycle = await cycleHandle.jsonValue()

  if (typeof cycle !== 'object' || cycle === null) {
    throw new Error('voice playback cycle was not measured')
  }

  const completedCycle = cycle as CompletedVoiceCycle
  const measuredValues = [
    completedCycle.sendAt,
    completedCycle.audioReceivedAt,
    completedCycle.audioDecodeAt,
    completedCycle.startedAt,
    completedCycle.latencyMs,
  ]
  if (!measuredValues.every((value) => Number.isFinite(value))) {
    throw new Error(`voice playback cycle contains non-finite values: ${JSON.stringify(completedCycle)}`)
  }
  if (completedCycle.audioReceivedAt < completedCycle.sendAt) {
    throw new Error(`voice audio response preceded audio send: ${JSON.stringify(completedCycle)}`)
  }
  if (completedCycle.audioDecodeAt < completedCycle.audioReceivedAt) {
    throw new Error(`voice audio decode preceded audio response: ${JSON.stringify(completedCycle)}`)
  }
  if (completedCycle.startedAt < completedCycle.audioDecodeAt) {
    throw new Error(`voice playback start preceded audio decode: ${JSON.stringify(completedCycle)}`)
  }
  if (completedCycle.latencyMs < 0) {
    throw new Error(`voice playback latency must be non-negative: ${JSON.stringify(completedCycle)}`)
  }

  return completedCycle
}

const waitForVoiceResponseFrameOrder = async (page: Page): Promise<string[]> => {
  const frameOrderHandle = await page.waitForFunction(
    () => {
      const expectedOrder = ['user-text', 'miori-text', 'audio']
      const frameOrder = window.__voiceChatE2E.frameOrder.slice(0, expectedOrder.length)

      if (frameOrder.length < expectedOrder.length) {
        return null
      }

      return frameOrder
    },
    undefined,
    { timeout: VOICE_RESPONSE_TIMEOUT_MS },
  )

  return frameOrderHandle.jsonValue() as Promise<string[]>
}

test('マイクボタン操作でOFFからSTANDBYへ遷移する', async ({ page }, testInfo) => {
  const micButton = await openVoiceChat(page, testInfo)

  await expect(micButton).not.toHaveClass(/mic-standby/)
  await expect(micButton).not.toHaveClass(/mic-active/)

  await micButton.click()

  await expect(micButton).toHaveAttribute('aria-pressed', 'true')
  await expect(micButton).toHaveClass(/mic-standby/)
})

test('VADの発話イベント中だけON表示になり、発話終了後にSTANDBYへ戻る', async ({ page }, testInfo) => {
  await enableMicrophone(page, testInfo)

  const micButton = await waitForSpeechDetection(page)

  await expect(micButton).toHaveClass(/mic-standby/, { timeout: 15000 })
  await expect(micButton).not.toHaveClass(/mic-active/)
})

test('音声応答のuser発話とmiori応答がこの順でチャット欄に表示される', async ({ page }, testInfo) => {
  await enableMicrophone(page, testInfo)
  await waitForSpeechCompletion(page)

  await expectVoiceChatMessages(page)
})

test('音声応答はuser text、miori text、audio binaryの順で受信する', async ({ page }, testInfo) => {
  await enableMicrophone(page, testInfo)
  await waitForSpeechCompletion(page)

  await expectVoiceChatMessages(page)
  await expect(waitForVoiceResponseFrameOrder(page)).resolves.toEqual([
    'user-text',
    'miori-text',
    'audio',
  ])
})

test('音声応答のバイナリフレームを受信するとブラウザで音声再生を開始する', async ({ page }, testInfo) => {
  await enableMicrophone(page, testInfo)
  await waitForSpeechCompletion(page)

  await waitForCompletedVoiceCycle(page)
})

test('音声送信から音声再生開始までの遅延を計測してレポートへ添付する', async ({ page }, testInfo) => {
  await enableMicrophone(page, testInfo)
  await waitForSpeechCompletion(page)

  const completedCycle = await waitForCompletedVoiceCycle(page)

  console.log(`voice playback latency: ${completedCycle.latencyMs.toFixed(2)} ms`)
  await testInfo.attach('voice-playback-latency.json', {
    body: JSON.stringify(completedCycle, null, 2),
    contentType: 'application/json',
  })
})
