import { fileURLToPath } from 'node:url'

import { expect, type Page } from '@playwright/test'

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
const VOICE_RESPONSE_TIMEOUT_MS = 60_000
const VOICE_TEST_TIMEOUT_MS = 120_000

type CompletedVoiceCycle = {
  sendAt: number
  audioReceivedAt: number
  audioDecodeAt: number
  startedAt: number
  latencyMs: number
}

const installPlaybackProbe = async (page: Page) => {
  await page.addInitScript(() => {
    window.__voiceChatE2E = { cycles: [], frameOrder: [] }

    const isAudioFrame = (data: unknown): data is ArrayBuffer | ArrayBufferView | Blob => (
      data instanceof ArrayBuffer || ArrayBuffer.isView(data) || data instanceof Blob
    )
    const findCycle = (predicate: (cycle: Window['__voiceChatE2E']['cycles'][number]) => boolean) => (
      window.__voiceChatE2E.cycles.find(predicate)
    )

    const NativeWebSocket = WebSocket
    window.WebSocket = class WebSocketWithPlaybackProbe extends NativeWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        protocols === undefined ? super(url) : super(url, protocols)
        this.addEventListener('message', (event) => {
          if (typeof event.data === 'string') {
            const parsed = JSON.parse(event.data) as { type?: unknown; speaker?: unknown }
            if (parsed.type === 'text' && parsed.speaker === 'user') {
              window.__voiceChatE2E.frameOrder.push('user-text')
            } else if (parsed.type === 'text' && parsed.speaker === 'miori') {
              window.__voiceChatE2E.frameOrder.push('miori-text')
            }
          } else if (isAudioFrame(event.data)) {
            window.__voiceChatE2E.frameOrder.push('audio')
            const cycle = findCycle((candidate) => candidate.audioReceivedAt === null)
            if (cycle !== undefined) cycle.audioReceivedAt = performance.now()
          }
        })
      }

      send(data: string | ArrayBufferLike | Blob | ArrayBufferView) {
        if (isAudioFrame(data)) {
          window.__voiceChatE2E.cycles.push({
            sendAt: performance.now(),
            audioReceivedAt: null,
            audioDecodeAt: null,
            startedAt: null,
          })
        }
        super.send(data)
      }
    } as typeof WebSocket

    const originalDecode = AudioContext.prototype.decodeAudioData
    AudioContext.prototype.decodeAudioData = function decodeWithProbe(
      this: AudioContext,
      audioData: ArrayBuffer,
      successCallback?: DecodeSuccessCallback | null,
      errorCallback?: DecodeErrorCallback | null,
    ) {
      const cycle = findCycle((candidate) => (
        candidate.audioReceivedAt !== null && candidate.audioDecodeAt === null
      ))
      if (cycle !== undefined) cycle.audioDecodeAt = performance.now()
      return originalDecode.call(this, audioData, successCallback, errorCallback)
    }

    const originalCreateSource = AudioContext.prototype.createBufferSource
    AudioContext.prototype.createBufferSource = function createSourceWithProbe(this: AudioContext) {
      const source = originalCreateSource.call(this)
      const originalStart = source.start.bind(source)
      source.start = (when?: number, offset?: number, duration?: number) => {
        const cycle = findCycle((candidate) => (
          candidate.audioReceivedAt !== null &&
          candidate.audioDecodeAt !== null &&
          candidate.startedAt === null
        ))
        if (cycle !== undefined) cycle.startedAt = performance.now()
        originalStart(when, offset, duration)
      }
      return source
    }
  })
}

const waitForCompletedVoiceCycle = async (page: Page): Promise<CompletedVoiceCycle> => {
  const handle = await page.waitForFunction(() => {
    const cycle = window.__voiceChatE2E.cycles.find((candidate) => (
      candidate.audioReceivedAt !== null &&
      candidate.audioDecodeAt !== null &&
      candidate.startedAt !== null
    ))
    if (
      cycle?.audioReceivedAt === null ||
      cycle?.audioDecodeAt === null ||
      cycle?.startedAt === null ||
      cycle === undefined
    ) return null
    return {
      ...cycle,
      latencyMs: cycle.startedAt - cycle.sendAt,
    }
  }, undefined, { timeout: VOICE_RESPONSE_TIMEOUT_MS })
  const cycle = await handle.jsonValue() as CompletedVoiceCycle
  const ordered = (
    cycle.audioReceivedAt >= cycle.sendAt &&
    cycle.audioDecodeAt >= cycle.audioReceivedAt &&
    cycle.startedAt >= cycle.audioDecodeAt &&
    cycle.latencyMs >= 0
  )
  if (!Object.values(cycle).every(Number.isFinite) || !ordered) {
    throw new Error(`invalid voice playback cycle: ${JSON.stringify(cycle)}`)
  }
  return cycle
}

export const createVoiceTestUseOptions = () => ({
  launchOptions: { args: [
      '--use-fake-device-for-media-stream',
      '--use-fake-ui-for-media-stream',
      `--use-file-for-fake-audio-capture=${speechFixturePath}`,
    ] },
  permissions: ['microphone'] as ['microphone'],
})

export const voiceTestTimeout = VOICE_TEST_TIMEOUT_MS

export const createVoiceChatDriver = () => {
  const openVoiceChat = async (page: Page) => {
    await installPlaybackProbe(page)
    await page.goto('/')
    const button = page.getByRole('button', { name: /マイクを(オン|オフ)にする/ })
    await expect(button).toBeEnabled()
    return button
  }

  const enableMicrophone = async (page: Page) => {
    const button = await openVoiceChat(page)
    await button.click()
    await expect(button).toHaveAttribute('aria-pressed', 'true')
    return button
  }

  const expectMicrophoneStandby = async (page: Page) => {
    const button = page.getByRole('button', { name: /マイクを(オン|オフ)にする/ })
    await expect.poll(
      () => button.evaluate((element) => ({
        active: element.classList.contains('mic-active'),
        standby: element.classList.contains('mic-standby'),
      })),
      { timeout: 15_000 },
    ).toEqual({ active: false, standby: true })
  }

  const waitForSpeechCompletion = async (page: Page) => {
    const button = page.getByRole('button', { name: /マイクを(オン|オフ)にする/ })
    await expect(button).toHaveClass(/mic-active/, { timeout: 15_000 })
    await page.waitForFunction(
      () => window.__voiceChatE2E.cycles.length > 0,
      undefined,
      { timeout: 15_000 },
    )
  }

  const expectMessages = async (page: Page) => {
    const messages = page.locator('article.message')
    await expect.poll(async () => messages.count(), { timeout: VOICE_RESPONSE_TIMEOUT_MS })
      .toBeGreaterThanOrEqual(2)
    await expect(messages.nth(0).locator('.speaker')).toHaveText('あなた')
    await expect(messages.nth(1).locator('.speaker')).toHaveText('光織')
    await expect(messages.nth(0).locator('p')).not.toHaveText('')
    await expect(messages.nth(1).locator('p')).not.toHaveText('')
  }

  const waitForFrameOrder = async (page: Page) => {
    const handle = await page.waitForFunction(
      () => window.__voiceChatE2E.frameOrder.length >= 3
        ? window.__voiceChatE2E.frameOrder.slice(0, 3)
        : null,
      undefined,
      { timeout: VOICE_RESPONSE_TIMEOUT_MS },
    )
    return handle.jsonValue() as Promise<string[]>
  }

  return {
    openVoiceChat,
    enableMicrophone,
    expectMicrophoneStandby,
    waitForSpeechCompletion,
    expectMessages,
    waitForCompletedVoiceCycle,
    waitForFrameOrder,
  }
}
