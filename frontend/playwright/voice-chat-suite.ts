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
      liveKitOrder: string[]
      micStates: ('off' | 'standby' | 'active')[]
      localStopAt?: number
      cancelRequestedAt?: number
      stoppedResponseId?: string
      interruptions: {
        responseId: string
        speechStartedAtMs: number
        localPlaybackStoppedAtMs: number
        cancelConfirmedAtMs: number | null
      }[]
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
    window.__voiceChatE2E = {
      cycles: [],
      frameOrder: [],
      liveKitOrder: [],
      micStates: [],
      interruptions: [],
    }
    const appendOnce = (entry: string) => {
      if (!window.__voiceChatE2E.liveKitOrder.includes(entry)) {
        window.__voiceChatE2E.liveKitOrder.push(entry)
      }
    }
    const observeMicrophoneState = () => {
      const button = document.querySelector<HTMLButtonElement>(
        'button[aria-label^="マイクを"]',
      )
      if (button === null) return
      const state = button.classList.contains('mic-active')
        ? 'active'
        : button.classList.contains('mic-standby')
          ? 'standby'
          : 'off'
      const states = window.__voiceChatE2E.micStates
      if (states.at(-1) !== state) states.push(state)
    }
    window.addEventListener('DOMContentLoaded', () => {
      const observer = new MutationObserver(observeMicrophoneState)
      observer.observe(document.documentElement, {
        subtree: true,
        childList: true,
        attributes: true,
        attributeFilter: ['class', 'aria-label', 'aria-pressed'],
      })
      observeMicrophoneState()
    }, { once: true })
    let fixtureStartedAt: number | null = null
    const testPortTarget = window as typeof window & {
      __digitalSoulsVoiceSessionTestPort?: {
        createRoom?: (...args: never[]) => unknown
        observeRoom?: (observation: {
          renderedEnergy?: number
          activeResponseId?: string
          activeAudioGraphs?: number
          renderedSamples?: number
          speechStartedAtMs?: number
          localPlaybackStoppedAtMs?: number
          cancelConfirmedAtMs?: number
        }) => void
        receiveCoreEvent?: (event: {
          type: string
          session_id?: string
          utterance_id?: string
          response_id?: string
          source_utterance_ids?: string[]
          measurement?: string
        }) => void
      }
    }
    testPortTarget.__digitalSoulsVoiceSessionTestPort = {
      ...testPortTarget.__digitalSoulsVoiceSessionTestPort,
      observeRoom: (observation) => {
        if (
          observation.activeResponseId !== undefined
          && observation.activeResponseId !== ''
          && observation.speechStartedAtMs !== undefined
          && observation.localPlaybackStoppedAtMs !== undefined
        ) {
          window.__voiceChatE2E.interruptions.push({
            responseId: observation.activeResponseId,
            speechStartedAtMs: observation.speechStartedAtMs,
            localPlaybackStoppedAtMs: observation.localPlaybackStoppedAtMs,
            cancelConfirmedAtMs: null,
          })
        }
        if (
          observation.activeResponseId !== undefined
          && observation.cancelConfirmedAtMs !== undefined
        ) {
          const interruption = [...window.__voiceChatE2E.interruptions]
            .reverse()
            .find((candidate) => candidate.responseId === observation.activeResponseId)
          if (interruption !== undefined) {
            interruption.cancelConfirmedAtMs = observation.cancelConfirmedAtMs
          }
        }
        if ((observation.activeAudioGraphs ?? 0) > 0) appendOnce('room:audio-graph')
        if (
          (observation.renderedEnergy ?? 0) > 0
          && observation.activeResponseId !== undefined
          && observation.activeResponseId !== ''
        ) {
          appendOnce(`${observation.activeResponseId}:rendered-audio`)
          const cycle = window.__voiceChatE2E.cycles.find((candidate) => (
            candidate.responseId === observation.activeResponseId
            && candidate.startedAt === null
          ))
          if (cycle !== undefined) {
            const now = performance.now()
            cycle.audioReceivedAt = now
            cycle.audioDecodeAt = now
            cycle.startedAt = now
            cycle.receivedBytes = observation.renderedSamples ?? 1
          }
        }
      },
      receiveCoreEvent: (event) => {
        if (
          event.type === 'utterance_finalized'
          && event.utterance_id !== undefined
          && !window.__voiceChatE2E.cycles.some(
            (candidate) => candidate.utteranceId === event.utterance_id,
          )
        ) {
          const now = performance.now()
          window.__voiceChatE2E.cycles.push({
            fixtureStartedAt: fixtureStartedAt ?? now,
            sendAt: now,
            audioReceivedAt: null,
            audioDecodeAt: null,
            startedAt: null,
            sessionId: event.session_id ?? '',
            utteranceId: event.utterance_id,
            responseId: null,
            conversationId: localStorage.getItem('digital-souls:conversation:miori') ?? '',
            sentBytes: 1,
            receivedBytes: null,
          })
        }
        if (event.type === 'response_started' && event.response_id !== undefined) {
          const sourceIds = event.source_utterance_ids ?? []
          const cycle = window.__voiceChatE2E.cycles.find((candidate) => (
            candidate.responseId === null && sourceIds.includes(candidate.utteranceId)
          ))
          if (cycle !== undefined) cycle.responseId = event.response_id
        }
        const responseId = event.response_id
        if (responseId === undefined) return
        if (event.type === 'response_delta') appendOnce(`${responseId}:text-delta`)
        if (event.type === 'response_completed') appendOnce(`${responseId}:completed`)
        if (event.type === 'observation' && event.measurement === 'first_audio_out') {
          appendOnce(`${responseId}:first-audio-out`)
        }
      },
    }
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
      private readonly conversationId: string | null
      private pendingCorrelation: { sessionId: string; utteranceId: string } | null = null

      constructor(url: string | URL, protocols?: string | string[]) {
        protocols === undefined ? super(url) : super(url, protocols)
        const parsedUrl = new URL(String(url), window.location.href)
        const conversationId = parsedUrl.pathname.startsWith('/ws/')
          ? parsedUrl.searchParams.get('conversation_id')
          : null
        this.conversationId = conversationId
        if (conversationId === null) return
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
        if (this.conversationId === null) {
          super.send(data)
          return
        }
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
    await page.waitForFunction(() => {
      const states = window.__voiceChatE2E.micStates
      const activeIndex = states.indexOf('active')
      return activeIndex >= 0 && states.slice(activeIndex + 1).includes('standby')
    }, undefined, { timeout: 15_000 })
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
    await expect(messages.nth(1).locator('.speaker')).toHaveText(/光織/)
    await expect(messages.nth(0).locator('p')).not.toHaveText('')
    await expect(messages.nth(1).locator('p')).not.toHaveText('', {
      timeout: VOICE_RESPONSE_TIMEOUT_MS,
    })
  }

  const waitForCompletedVoiceCycles = async (page: Page, count: number) => {
    if (!Number.isInteger(count) || count < 1) {
      throw new Error('voice cycle count must be positive')
    }
    const handle = await page.waitForFunction((requiredCount) => {
      const completed = window.__voiceChatE2E.cycles.filter((cycle) => (
        cycle.audioReceivedAt !== null
        && cycle.audioDecodeAt !== null
        && cycle.startedAt !== null
        && cycle.responseId !== null
        && cycle.receivedBytes !== null
      ))
      return completed.length >= requiredCount ? completed.slice(0, requiredCount) : null
    }, count, { timeout: VOICE_RESPONSE_TIMEOUT_MS * count })
    return handle.jsonValue()
  }

  const waitForInterruptionEvidence = async (page: Page) => {
    const handle = await page.waitForFunction(() => {
      const evidence = window.__voiceChatE2E.interruptions.find((candidate) => (
        candidate.cancelConfirmedAtMs !== null
      ))
      return evidence ?? null
    }, undefined, { timeout: VOICE_RESPONSE_TIMEOUT_MS })
    return handle.jsonValue()
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

  const waitForLiveKitStreamingOrder = async (page: Page) => {
    try {
      const handle = await page.waitForFunction(() => {
        const entries = window.__voiceChatE2E.liveKitOrder
        for (const delta of entries.filter((entry) => entry.endsWith(':text-delta'))) {
          const responseId = delta.slice(0, -':text-delta'.length)
          const expected = [
            `${responseId}:text-delta`,
            `${responseId}:first-audio-out`,
            `${responseId}:completed`,
          ]
          const indices = expected.map((entry) => entries.indexOf(entry))
          const audioGraph = entries.includes('room:audio-graph')
          if (
            indices.every((index) => index >= 0)
            && indices[0] < indices[1]
            && indices[1] < indices[2]
            && audioGraph
          ) return expected.map((entry) => entry.slice(responseId.length + 1))
        }
        return null
      }, undefined, { timeout: VOICE_RESPONSE_TIMEOUT_MS })
      return handle.jsonValue() as Promise<string[]>
    } catch (error) {
      const observed = await page.evaluate(() => window.__voiceChatE2E.liveKitOrder)
      throw new Error(`LiveKit streaming order incomplete: ${JSON.stringify(observed)}`, {
        cause: error,
      })
    }
  }

  const endVoiceSession = async (page: Page) => {
    const button = page.getByRole('button', { name: '音声会話を終了' })
    if (await button.count() === 0 || !await button.isVisible()) return
    await button.click()
    await expect(button).toBeHidden()
  }

  return {
    openVoiceChat,
    enableMicrophone,
    expectMicrophoneStandby,
    waitForSpeechCompletion,
    expectMessages,
    readUserTranscript,
    waitForCompletedVoiceCycle,
    waitForCompletedVoiceCycles,
    waitForInterruptionEvidence,
    waitForFrameOrder,
    waitForLiveKitStreamingOrder,
    endVoiceSession,
  }
}
