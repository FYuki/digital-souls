import { expect, test } from '@playwright/test'

import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../../playwright/resolved-profile'
import { hardDeleteSelectedConversation } from '../../playwright/conversation-cleanup'
import {
  createVoiceChatDriver,
  createVoiceTestUseOptions,
  voiceTestTimeout,
} from '../../playwright/voice-chat-suite'

let resolvedProfile: ResolvedProfile

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({}, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'voice-chat-real')
  if (reason !== null) test.skip(true, reason)
})

test.afterEach(async ({ page, context }) => {
  await context.setOffline(false)
  await driver.endVoiceSession(page)
  await hardDeleteSelectedConversation(page, 'miori')
})

test.use(createVoiceTestUseOptions())
test.describe.configure({ mode: 'serial' })
test.setTimeout(voiceTestTimeout)

const driver = createVoiceChatDriver()

test('マイクボタン操作でOFFからSTANDBYへ遷移する', async ({ page }) => {
  const button = await driver.openVoiceChat(page)
  await expect(button).not.toHaveClass(/mic-standby|mic-active/)
  const tokenResponse = page.waitForResponse((response) => (
    response.url().endsWith('/api/voice/livekit/token')
    && response.request().method() === 'POST'
  ))
  await button.click()
  expect((await tokenResponse).ok()).toBe(true)
  await expect(button).toHaveClass(/mic-standby/)
})

test('VAD発話終了後もLiveKit継続microphone sessionを維持する', async ({ page }) => {
  const button = await driver.enableMicrophone(page)
  await expect(button).toHaveClass(/mic-active/, { timeout: 15_000 })
  await driver.expectMicrophoneStandby(page)
  await expect(button).toHaveAttribute('aria-pressed', 'true')
})

test('実音声応答のuser発話とmiori応答がこの順でチャット欄に表示される', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.expectMessages(page)
  await expect(driver.waitForLiveKitStreamingOrder(page)).resolves.toEqual([
    'text-delta',
    'first-audio-out',
    'completed',
  ])
})

test('通常UIの同一LiveKit sessionで実サービス応答を3往復継続する', async ({ page }) => {
  test.setTimeout(voiceTestTimeout * 3)
  await driver.enableMicrophone(page)
  await driver.waitForCompletedVoiceCycles(page, 3)

  await expect(page.locator('article.message')).toHaveCount(6)
  await expect(page.getByRole('button', { name: 'マイクをオフにする' }))
    .toHaveAttribute('aria-pressed', 'true')
})

test('実LiveKit barge-inのlocal停止とcancel確定latencyを記録する', async ({ page }, testInfo) => {
  test.setTimeout(voiceTestTimeout * 2)
  await driver.enableMicrophone(page)
  await expect(page.getByText('応答: 応答生成中')).toBeVisible({
    timeout: voiceTestTimeout,
  })
  await page.evaluate(async () => {
    const controller = window.__voiceSessionController
    if (controller === undefined) throw new Error('voice controller is required')
    await controller.speechStarted(crypto.randomUUID(), performance.now())
  })
  const evidence = await driver.waitForInterruptionEvidence(page) as {
    responseId: string
    speechStartedAtMs: number
    localPlaybackStoppedAtMs: number
    cancelConfirmedAtMs: number
  }
  const localStopMs = evidence.localPlaybackStoppedAtMs - evidence.speechStartedAtMs
  const cancelTotalMs = evidence.cancelConfirmedAtMs - evidence.speechStartedAtMs

  expect(localStopMs).toBeGreaterThanOrEqual(0)
  // 冒頭STTで相槌を除外してから停止するため、VAD直後の即時停止は要求しない。
  expect(localStopMs).toBeLessThanOrEqual(3_000)
  expect(cancelTotalMs).toBeGreaterThanOrEqual(0)
  expect(cancelTotalMs).toBeLessThanOrEqual(3_500)
  await testInfo.attach('barge-in-latency.real.json', {
    body: JSON.stringify({
      source: 'automated_test',
      localStopMs,
      cancelTotalMs,
      responseId: evidence.responseId,
    }, null, 2),
    contentType: 'application/json',
  })
})

test('通常UIが実LiveKit一時切断から同じconversationへ復帰する', async ({ page, context }, testInfo) => {
  await driver.enableMicrophone(page)
  const disconnectedAtMs = await page.evaluate(() => performance.now())
  await context.setOffline(true)
  await expect(page.getByText('セッション: 再接続中')).toBeVisible({ timeout: 15_000 })
  await context.setOffline(false)
  await expect(page.getByText('セッション: 接続済み')).toBeVisible({ timeout: 60_000 })
  const reconnectedAtMs = await page.evaluate(() => performance.now())

  await expect(page.locator('section[aria-label="音声会話の状態"]')).toHaveCount(1)
  await testInfo.attach('reconnect-latency.real.json', {
    body: JSON.stringify({
      source: 'automated_test',
      reconnectMs: reconnectedAtMs - disconnectedAtMs,
    }, null, 2),
    contentType: 'application/json',
  })
})
