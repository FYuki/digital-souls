import { fileURLToPath } from 'node:url'

import { expect, type Page } from '@playwright/test'

declare global {
  interface Window {
    __voiceChatE2E: {
      cycles: {
        fixtureStartedAt: number
        sendAt: number
        audioReceivedAt: number | null
        audioDecodeAt: number | null
        startedAt: number | null
        sessionId: string
        utteranceId: string
        responseId: string | null
        conversationId: string
        sentBytes: number
        receivedBytes: number | null
      }[]
      frameOrder: string[]
    }
  }
}

const speechFixturePath = fileURLToPath(new URL('./fixtures/speech.wav', import.meta.url))
const VOICE_RESPONSE_TIMEOUT_MS = 60_000
const VOICE_TEST_TIMEOUT_MS = 120_000

type CompletedVoiceCycle = {
  fixtureStartedAt: number
  sendAt: number
  audioReceivedAt: number
  audioDecodeAt: number
  startedAt: number
  latencyMs: number
  sessionId: string
  utteranceId: string
  responseId: string
  conversationId: string
  sentBytes: number
  receivedBytes: number
}

const installPlaybackProbe = async (page: Page) => {
  await page.addInitScript(() => {
    window.__voiceChatE2E = { cycles: [], frameOrder: [] }
    let fixtureStartedAt: number | null = null
    const nativeGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
    navigator.mediaDevices.getUserMedia = async (constraints) => {
      const stream = await nativeGetUserMedia(constraints)
      fixtureStartedAt = performance.now()
      return stream
    }

    const isAudioFrame = (data: unknown): data is ArrayBuffer | ArrayBufferView | Blob => (
      data instanceof ArrayBuffer || ArrayBuffer.isView(data) || data instanceof Blob
    )
    const findCycle = (predicate: (cycle: Window['__voiceChatE2E']['cycles'][number]) => boolean) => (
      window.__voiceChatE2E.cycles.find(predicate)
    )

    const NativeWebSocket = WebSocket
    window.WebSocket = class WebSocketWithPlaybackProbe extends NativeWebSocket {
      private readonly conversationId: string
      private pendingCorrelation: { sessionId: string; utteranceId: string } | null = null

      constructor(url: string | URL, protocols?: string | string[]) {
        protocols === undefined ? super(url) : super(url, protocols)
        const conversationId = new URL(String(url)).searchParams.get('conversation_id')
        if (conversationId === null) throw new Error('voice WebSocket conversation_id is required')
        this.conversationId = conversationId
        this.addEventListener('message', (event) => {
          if (typeof event.data === 'string') {
            const parsed = JSON.parse(event.data) as {
              type?: unknown
              turn?: unknown
              response_id?: unknown
            }
            if (parsed.type === 'text' && parsed.turn !== undefined) {
              window.__voiceChatE2E.frameOrder.push('persisted-turn')
            }
            if (parsed.type === 'audio_response_metadata' && typeof parsed.response_id === 'string') {
              const cycle = findCycle((candidate) => candidate.responseId === null)
              if (cycle !== undefined) cycle.responseId = parsed.response_id
            }
          } else if (isAudioFrame(event.data)) {
            window.__voiceChatE2E.frameOrder.push('audio')
            const cycle = findCycle((candidate) => candidate.audioReceivedAt === null)
            if (cycle !== undefined) {
              cycle.audioReceivedAt = performance.now()
              cycle.receivedBytes = event.data instanceof Blob
                ? event.data.size
                : event.data instanceof ArrayBuffer
                  ? event.data.byteLength
                  : event.data.byteLength
            }
          }
        })
      }

      send(data: string | ArrayBufferLike | Blob | ArrayBufferView) {
        if (typeof data === 'string') {
          const parsed = JSON.parse(data) as {
            type?: unknown
            session_id?: unknown
            utterance_id?: unknown
          }
          if (
            parsed.type === 'audio_metadata'
            && typeof parsed.session_id === 'string'
            && typeof parsed.utterance_id === 'string'
          ) {
            this.pendingCorrelation = {
              sessionId: parsed.session_id,
              utteranceId: parsed.utterance_id,
            }
          }
        }
        if (isAudioFrame(data)) {
          if (fixtureStartedAt === null || this.pendingCorrelation === null) {
            throw new Error('voice fixture and audio correlation must be initialized')
          }
          window.__voiceChatE2E.cycles.push({
            fixtureStartedAt,
            sendAt: performance.now(),
            audioReceivedAt: null,
            audioDecodeAt: null,
            startedAt: null,
            sessionId: this.pendingCorrelation.sessionId,
            utteranceId: this.pendingCorrelation.utteranceId,
            responseId: null,
            conversationId: this.conversationId,
            sentBytes: data instanceof Blob ? data.size : data.byteLength,
            receivedBytes: null,
          })
          this.pendingCorrelation = null
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
      cycle?.responseId === null ||
      cycle?.receivedBytes === null ||
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
  const numericValues = [
    cycle.fixtureStartedAt,
    cycle.sendAt,
    cycle.audioReceivedAt,
    cycle.audioDecodeAt,
    cycle.startedAt,
    cycle.latencyMs,
    cycle.sentBytes,
    cycle.receivedBytes,
  ]
  const correlationComplete = [
    cycle.sessionId,
    cycle.utteranceId,
    cycle.responseId,
    cycle.conversationId,
  ].every((value) => value.length > 0)
  if (!numericValues.every(Number.isFinite) || !ordered || !correlationComplete) {
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
    await page.getByRole('button', { name: '新規スレッド' }).click()
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

  const readUserTranscript = async (page: Page): Promise<string> => {
    const transcript = await page.locator('article.message').nth(0).locator('p').textContent()
    if (transcript === null) throw new Error('user transcript is not available')
    return transcript
  }

  const waitForFrameOrder = async (page: Page) => {
    const handle = await page.waitForFunction(
      () => window.__voiceChatE2E.frameOrder.length >= 2
        ? window.__voiceChatE2E.frameOrder.slice(0, 2)
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
    readUserTranscript,
    waitForCompletedVoiceCycle,
    waitForFrameOrder,
  }
}
