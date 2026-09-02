import { expect, test, type Page } from '@playwright/test'

import {
  createVoiceChatDriver,
  createVoiceTestUseOptions,
  voiceTestTimeout,
} from '../playwright/voice-chat-suite'
import { installMockLiveKit } from './mock-livekit'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../playwright/resolved-profile'

const MOCK_TRANSCRIPT_TEXT = 'テスト音声です'
const MOCK_RESPONSE_TEXT = 'テスト音声に応答します。'
const MOCK_CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
let resolvedProfile: ResolvedProfile

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({ page }, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'mocked-e2e')
  if (reason !== null) test.skip(true, reason)
  await installMockBackend(page)
})

const installMockBackend = async (page: Page) => {
  const liveKit = await installMockLiveKit(page, {
    transcript: MOCK_TRANSCRIPT_TEXT,
    response: MOCK_RESPONSE_TEXT,
  })
  await page.route('**/api/characters/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.endsWith('/turns')) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(liveKit.readTurns()),
      })
      return
    }
    const conversation = {
      character_id: 'miori',
      conversation_id: MOCK_CONVERSATION_ID,
      created_at: '2026-08-01T12:00:00.000000Z',
      updated_at: '2026-08-01T12:00:00.000000Z',
      archived_at: null,
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(request.method() === 'POST' ? conversation : []),
    })
  })
}

test.use(createVoiceTestUseOptions())
test.describe.configure({ mode: 'serial' })
test.setTimeout(voiceTestTimeout)

const driver = createVoiceChatDriver()

const expectMockMessages = async (page: Page) => {
  const messages = page.locator('article.message')
  await expect(messages.nth(0).locator('p')).toHaveText(MOCK_TRANSCRIPT_TEXT)
  await expect(messages.nth(1).locator('p')).toHaveText(MOCK_RESPONSE_TEXT)
}

test('マイクボタン操作でOFFから有効状態へ遷移する', async ({ page }) => {
  const button = await driver.openVoiceChat(page)
  await expect(button).not.toHaveClass(/mic-standby|mic-active/)
  await button.click()
  await expect(button).toHaveAttribute('aria-pressed', 'true')
  await expect(button).toHaveClass(/mic-standby|mic-active/)
})

test('VADの発話イベント中も継続microphone sessionを維持する', async ({ page }) => {
  const button = await driver.enableMicrophone(page)
  await expect(button).toHaveClass(/mic-active/, { timeout: 15_000 })
  await page.waitForFunction(() => window.__voiceChatE2E.cycles.length > 0)
  await expect(button).toHaveAttribute('aria-pressed', 'true')
})

test('音声応答のuser発話とmiori応答がこの順でチャット欄に表示される', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.expectMessages(page)
  await expectMockMessages(page)
})

test('音声応答はtext delta、audio segmentの順で受信する', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.expectMessages(page)
  await expect(driver.waitForFrameOrder(page)).resolves.toEqual(['text-delta', 'audio'])
})

test('音声segmentを受信すると再生開始観測をresponseへ相関する', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.waitForCompletedVoiceCycle(page)
})

test('音声送信から音声再生開始までの遅延を計測してレポートへ添付する', async ({ page }, testInfo) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  const cycle = await driver.waitForCompletedVoiceCycle(page)
  await testInfo.attach('voice-playback-latency.json', {
    body: JSON.stringify(cycle, null, 2),
    contentType: 'application/json',
  })
})

test('通常UIの同一sessionで追加操作なしに3往復を履歴へ確定する', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await expect(page.locator('article.message')).toHaveCount(2)
  await expect(page.getByText('応答: 待機')).toBeVisible()

  for (let index = 0; index < 2; index += 1) {
    await page.evaluate(async () => {
      const mock = (window as unknown as { __mockLiveKit?: {
        submitUtterance: () => Promise<void>
      } }).__mockLiveKit
      if (mock === undefined) throw new Error('mock LiveKit control is required')
      await mock.submitUtterance()
    })
    await expect(page.locator('article.message')).toHaveCount((index + 2) * 2)
  }

  await expect(page.getByText(MOCK_TRANSCRIPT_TEXT, { exact: true })).toHaveCount(3)
  await expect(page.getByText(MOCK_RESPONSE_TEXT, { exact: true })).toHaveCount(3)
  await expect(page.getByRole('button', { name: 'マイクをオフにする' }))
    .toHaveAttribute('aria-pressed', 'true')
})

test('通常UIでmute・再開・終了しRoomとmicrophone resourceを一度ずつ解放する', async ({ page }) => {
  const button = await driver.enableMicrophone(page)

  await button.click()
  await expect(button).toHaveAttribute('aria-pressed', 'false')
  await expect(page.getByText('入力: ミュート')).toBeVisible()

  await button.click()
  await expect(button).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText('セッション: 接続済み')).toBeVisible()

  await page.getByRole('button', { name: '音声会話を終了' }).click()
  await expect(page.getByRole('button', { name: '音声会話を終了' })).toBeHidden()
  await expect(page.getByText('セッション: 終了')).toBeVisible()
  await expect(page.getByText('入力: 停止')).toBeVisible()
  await expect(button).toHaveAttribute('aria-pressed', 'false')

  await expect.poll(() => page.evaluate(() => {
    const lifecycle = (window as unknown as { __mockLiveKit?: {
      lifecycle: {
        publishMicrophoneCount: number
        muteMicrophoneCount: number
        disconnectCount: number
      }
    } }).__mockLiveKit?.lifecycle
    return lifecycle
  })).toEqual({
    publishMicrophoneCount: 2,
    muteMicrophoneCount: 1,
    disconnectCount: 1,
  })
})

test('一時切断中の状態を表示し同じ通常UIへ重複なく復帰する', async ({ page }, testInfo) => {
  await driver.enableMicrophone(page)
  const disconnectedAtMs = await page.evaluate(() => {
    const mock = (window as unknown as { __mockLiveKit?: {
      disconnect: () => void
    } }).__mockLiveKit
    if (mock === undefined) throw new Error('mock LiveKit control is required')
    mock.disconnect()
    return performance.now()
  })
  await expect(page.getByText('セッション: 再接続中')).toBeVisible()
  await expect(page.getByText('接続を復旧しています。会話履歴は保持されます。'))
    .toBeVisible()

  const reconnectedAtMs = await page.evaluate(() => {
    const mock = (window as unknown as { __mockLiveKit?: {
      reconnect: () => void
    } }).__mockLiveKit
    if (mock === undefined) throw new Error('mock LiveKit control is required')
    mock.reconnect()
    return performance.now()
  })

  await expect(page.getByText('セッション: 接続済み')).toBeVisible()
  await expect(page.locator('section[aria-label="音声会話の状態"]')).toHaveCount(1)
  await testInfo.attach('reconnect-latency.mock.json', {
    body: JSON.stringify({
      source: 'automated_test',
      reconnectMs: reconnectedAtMs - disconnectedAtMs,
      duplicateStateRegions: 0,
    }, null, 2),
    contentType: 'application/json',
  })
})

test('barge-inでlocal停止とserver cancelを相関し遅延出力を破棄する', async ({ page }, testInfo) => {
  await driver.enableMicrophone(page)
  const responseId = await page.evaluate(() => {
    const mock = (window as unknown as { __mockLiveKit?: {
      beginInterruptibleResponse: () => string
    } }).__mockLiveKit
    if (mock === undefined) throw new Error('mock LiveKit control is required')
    return mock.beginInterruptibleResponse()
  })
  await page.evaluate(async () => {
    const controller = window.__voiceSessionController
    if (controller === undefined) throw new Error('mock voice controller is required')
    await controller.speechStarted(crypto.randomUUID(), performance.now())
  })

  const evidence = await driver.waitForInterruptionEvidence(page) as {
    responseId: string
    speechStartedAtMs: number
    localPlaybackStoppedAtMs: number
    cancelConfirmedAtMs: number
  }
  expect(evidence.responseId).toBe(responseId)
  expect(evidence.localPlaybackStoppedAtMs - evidence.speechStartedAtMs).toBeLessThanOrEqual(150)
  expect(evidence.cancelConfirmedAtMs).toBeGreaterThanOrEqual(
    evidence.localPlaybackStoppedAtMs,
  )
  await expect(page.getByText('破棄対象', { exact: true })).toHaveCount(0)
  const messageCountBeforeNextTurn = await page.locator('article.message').count()
  await page.evaluate(async () => {
    const mock = (window as unknown as { __mockLiveKit?: {
      submitUtterance: () => Promise<void>
    } }).__mockLiveKit
    if (mock === undefined) throw new Error('mock LiveKit control is required')
    await mock.submitUtterance()
  })
  await expect(page.locator('article.message')).toHaveCount(messageCountBeforeNextTurn + 2)
  await expect(page.getByRole('button', { name: 'マイクをオフにする' }))
    .toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText('破棄対象', { exact: true })).toHaveCount(0)
  await testInfo.attach('barge-in-latency.mock.json', {
    body: JSON.stringify({ source: 'automated_test', ...evidence }, null, 2),
    contentType: 'application/json',
  })
})

test('連続barge-in後も旧responseを混入させず同じsessionで次の発話を処理する', async ({ page }) => {
  await driver.enableMicrophone(page)

  const interrupt = async () => page.evaluate(async () => {
    const mock = (window as unknown as { __mockLiveKit?: {
      beginInterruptibleResponse: () => string
    } }).__mockLiveKit
    const controller = window.__voiceSessionController
    if (mock === undefined || controller === undefined) {
      throw new Error('mock LiveKit and voice controller are required')
    }
    const responseId = mock.beginInterruptibleResponse()
    await controller.speechStarted(crypto.randomUUID(), performance.now())
    return responseId
  })

  const firstResponseId = await interrupt()
  await page.waitForFunction(() => (
    window.__voiceChatE2E.interruptions.filter(
      (candidate) => candidate.cancelConfirmedAtMs !== null,
    ).length >= 1
  ))
  const secondResponseId = await interrupt()
  await page.waitForFunction(() => (
    window.__voiceChatE2E.interruptions.filter(
      (candidate) => candidate.cancelConfirmedAtMs !== null,
    ).length >= 2
  ))

  expect(secondResponseId).not.toBe(firstResponseId)
  await expect(page.getByText('破棄対象', { exact: true })).toHaveCount(0)
  const messageCountBeforeNextTurn = await page.locator('article.message').count()
  await page.evaluate(async () => {
    const mock = (window as unknown as { __mockLiveKit?: {
      submitUtterance: () => Promise<void>
    } }).__mockLiveKit
    if (mock === undefined) throw new Error('mock LiveKit control is required')
    await mock.submitUtterance()
  })
  await expect(page.locator('article.message')).toHaveCount(messageCountBeforeNextTurn + 2)
  await expect(page.getByRole('button', { name: 'マイクをオフにする' }))
    .toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText('破棄対象', { exact: true })).toHaveCount(0)
})
