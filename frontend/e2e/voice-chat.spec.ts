import { expect, test, type Page } from '@playwright/test'

import {
  createVoiceChatDriver,
  createVoiceTestUseOptions,
  voiceTestTimeout,
} from '../playwright/voice-chat-suite'
import { installMockLiveKit } from './mock-livekit'
import { installMockUiBootstrap } from './mock-ui-bootstrap'
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
      title: MOCK_CONVERSATION_ID,
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(request.method() === 'POST' ? conversation : []),
    })
  })
  await installMockUiBootstrap(page)
}

test.use(createVoiceTestUseOptions())
test.setTimeout(voiceTestTimeout)

const driver = createVoiceChatDriver()

// BE通知を代入するモックE2E。実音声の自動検知・性能受入はintegrationが担当する。
const submitMockSpeech = async (page: Page) => {
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as {__mockLiveKit: {microphoneEnabled: () => boolean}})
      .__mockLiveKit.microphoneEnabled(),
  )).toBe(true)
  await page.evaluate(async () => (window as unknown as {__mockLiveKit: {
    submitUtterance: () => Promise<void>
  }}).__mockLiveKit.submitUtterance())
}

const interruptionEvidence = async (page: Page) => {
  return page.evaluate(() => (window as unknown as {__mockLiveKit: {interruptions: {
    responseId: string
    backendDecisionAtMs: number
    localPlaybackStoppedAtMs: number | null
    cancelConfirmedAtMs: number | null
  }[]}}).__mockLiveKit.interruptions)
}

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

for (const operation of ['Enter', 'click'] as const) {
  for (const manualMute of [false, true]) {
    test(`focusだけで音声入力を抑止し${operation}受理後もmanual mute=${manualMute}を保持する`, async ({page}) => {
      const button = await driver.enableMicrophone(page)
      if (manualMute) {
        await button.click()
        await expect(page.getByText('入力: ミュート')).toBeVisible()
      }
      await page.evaluate(() => (window as unknown as {__mockLiveKit: {beginInterruptibleResponse: () => void}}).__mockLiveKit.beginInterruptibleResponse())
      const input = page.getByLabel('メッセージ')
      await input.fill('文字入力を優先')
      await expect(input).toBeFocused()
      await expect(page.getByText(manualMute ? '入力: ミュート' : '入力: テキスト入力中')).toBeVisible()
      await expect(page.getByText('再生: 再生中')).toBeVisible()
      if (!manualMute) await expect.poll(() => page.evaluate(() =>
        (window as unknown as {__mockLiveKit: {microphoneEnabled: () => boolean}}).__mockLiveKit.microphoneEnabled(),
      )).toBe(false)
      if (operation === 'Enter') await input.press('Enter')
      else await page.getByRole('button', {name: '送信', exact: true}).click()
      await expect(page.getByText('送信中', {exact: true})).toBeVisible()
      await expect(input).toBeFocused()
      await page.evaluate(() => (window as unknown as {__mockLiveKit: {resolveTextInput: (status: 'accepted') => void}}).__mockLiveKit.resolveTextInput('accepted'))
      await expect(input).not.toBeFocused()
      await expect(input).toHaveValue('')
      await expect(page.getByText(manualMute ? '入力: ミュート' : '入力: 聞き取り中')).toBeVisible()
      await expect.poll(() => page.evaluate(() =>
        (window as unknown as {__mockLiveKit: {lifecycle: {publishMicrophoneCount: number}}}).__mockLiveKit.lifecycle.publishMicrophoneCount,
      )).toBe(manualMute ? 1 : 2)
    })
  }
}

test('BEの発話通知中も継続microphone sessionを維持する', async ({ page }) => {
  const button = await driver.enableMicrophone(page)
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as {__mockLiveKit: {microphoneEnabled: () => boolean}})
      .__mockLiveKit.microphoneEnabled(),
  )).toBe(true)
  const utteranceId = await page.evaluate(() => (window as unknown as {__mockLiveKit: {
    beginSpeech: () => string
  }}).__mockLiveKit.beginSpeech())
  await expect(button).toHaveClass(/mic-standby/)
  await expect(button).toHaveAttribute('aria-pressed', 'true')
  await page.evaluate(id => (window as unknown as {__mockLiveKit: {
    finishSpeech: (id: string) => void
  }}).__mockLiveKit.finishSpeech(id), utteranceId)
  await expect(page.getByText('入力: 文字起こし中')).toBeVisible()
  const clientTypes = await page.evaluate(() => (window as unknown as {__mockLiveKit: {
    controlEvents: {type: string}[]
  }}).__mockLiveKit.controlEvents.map(event => event.type))
  expect(clientTypes).not.toContain('speech_started')
  expect(clientTypes).not.toContain('speech_stopped')
})

test('音声応答のuser発話とmiori応答がこの順でチャット欄に表示される', async ({ page }) => {
  await driver.enableMicrophone(page)
  await submitMockSpeech(page)
  await driver.expectMessages(page)
  await expectMockMessages(page)
})

test('音声応答はtext delta、audio segmentの順で受信する', async ({ page }) => {
  await driver.enableMicrophone(page)
  await submitMockSpeech(page)
  await driver.expectMessages(page)
  await expect(driver.waitForFrameOrder(page)).resolves.toEqual(['text-delta', 'audio'])
})

test('音声segmentを受信すると再生開始観測をresponseへ相関する', async ({ page }) => {
  await driver.enableMicrophone(page)
  await submitMockSpeech(page)
  await driver.waitForCompletedVoiceCycle(page)
})

test('モックBE通知とモック再生開始の相関をレポートへ添付する', async ({ page }, testInfo) => {
  await driver.enableMicrophone(page)
  await submitMockSpeech(page)
  const cycle = await driver.waitForCompletedVoiceCycle(page)
  await testInfo.attach('voice-playback-latency.json', {
    body: JSON.stringify({source: 'mock_backend_notification', ...cycle}, null, 2),
    contentType: 'application/json',
  })
})

test('FE VAD assetなしで同一sessionへのBEモック通知3往復を履歴へ確定する', async ({ page }) => {
  let vadAssetRequests = 0
  await page.route('**/vad-assets/**', route => {
    vadAssetRequests += 1
    return route.abort()
  })
  await driver.enableMicrophone(page)
  await submitMockSpeech(page)
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

  expect(vadAssetRequests).toBe(0)
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
    muteMicrophoneCount: 3,
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
    ;(window as unknown as {__mockLiveKit: {interruptFromBackend: () => void}})
      .__mockLiveKit.interruptFromBackend()
  })

  const [evidence] = await interruptionEvidence(page)
  expect(evidence.responseId).toBe(responseId)
  expect(evidence.localPlaybackStoppedAtMs).not.toBeNull()
  expect(evidence.localPlaybackStoppedAtMs! - evidence.backendDecisionAtMs).toBeLessThanOrEqual(150)
  expect(evidence.cancelConfirmedAtMs).toBeGreaterThanOrEqual(evidence.localPlaybackStoppedAtMs!)
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
    body: JSON.stringify({ source: 'mock_backend_notification', ...evidence }, null, 2),
    contentType: 'application/json',
  })
})

test('text submitが旧回答を停止し、遅延deltaを混ぜず同じsessionで次の音声へ戻る', async ({page}) => {
  await driver.enableMicrophone(page)
  const oldResponseId = await page.evaluate(() =>
    (window as unknown as {__mockLiveKit: {beginInterruptibleResponse: () => string}}).__mockLiveKit.beginInterruptibleResponse(),
  )
  const input = page.getByLabel('メッセージ')
  await input.fill('テキストによる割り込み')
  await expect(page.getByText('再生: 再生中')).toBeVisible()
  await input.press('Enter')
  await expect(page.getByText('再生: 停止済み')).toBeVisible()
  await expect(page.getByText('破棄対象', {exact: true})).toHaveCount(0)
  const types = await page.evaluate(() =>
    (window as unknown as {__mockLiveKit: {controlEvents: {type: string}[]}}).__mockLiveKit.controlEvents.map(event => event.type),
  )
  expect(types).toEqual(expect.arrayContaining(['playback_stopped', 'response_cancel_requested', 'user_text_submitted']))
  await page.evaluate(async oldId => {
    const mock = (window as unknown as {__mockLiveKit: {
      completeTextResponse: (text: string) => Promise<string>
      emitLateOutput: (responseId: string) => void
    }}).__mockLiveKit
    await mock.completeTextResponse('テキストへの新しい回答')
    mock.emitLateOutput(oldId)
  }, oldResponseId)
  await expect(page.getByText('テキストによる割り込み', {exact: true})).toBeVisible()
  await expect(page.getByText('テキストへの新しい回答', {exact: true})).toBeVisible()
  await expect(page.getByText('破棄対象', {exact: true})).toHaveCount(0)
  await expect(input).not.toBeFocused()
  await expect(page.getByText('入力: 聞き取り中')).toBeVisible()
  await page.evaluate(async () => (window as unknown as {__mockLiveKit: {submitUtterance: () => Promise<void>}}).__mockLiveKit.submitUtterance())
  await expect(page.getByText(MOCK_RESPONSE_TEXT, {exact: true}).last()).toBeVisible()
  await expect(page.getByText('破棄対象', {exact: true})).toHaveCount(0)
  const lifecycle = await page.evaluate(() =>
    (window as unknown as {__mockLiveKit: {lifecycle: {publishMicrophoneCount: number; disconnectCount: number}}}).__mockLiveKit.lifecycle,
  )
  expect(lifecycle.publishMicrophoneCount).toBe(2)
  expect(lifecycle.disconnectCount).toBe(0)
})

test('連続barge-in後も旧responseを混入させず同じsessionで次の発話を処理する', async ({ page }) => {
  await driver.enableMicrophone(page)

  const interrupt = async () => page.evaluate(async () => {
    const mock = (window as unknown as { __mockLiveKit?: {
      beginInterruptibleResponse: () => string
      interruptFromBackend: () => void
    } }).__mockLiveKit
    if (mock === undefined) throw new Error('mock LiveKit is required')
    const responseId = mock.beginInterruptibleResponse()
    mock.interruptFromBackend()
    return responseId
  })

  const firstResponseId = await interrupt()
  expect((await interruptionEvidence(page)).filter(row => row.cancelConfirmedAtMs !== null)).toHaveLength(1)
  const secondResponseId = await interrupt()
  expect((await interruptionEvidence(page)).filter(row => row.cancelConfirmedAtMs !== null)).toHaveLength(2)

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
