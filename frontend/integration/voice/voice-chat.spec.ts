import { installResponseTrackDiagnostic } from '../../playwright/response-track-readiness-diagnostic'
import { readFileSync } from 'node:fs'
import { installScheduledFixture, parseScheduledFixture } from '../../playwright/controlled-audio-fixture'
import { expect, test, type Page } from '@playwright/test'

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

test.beforeEach(async ({ page }, testInfo) => {
  await page.addInitScript(installResponseTrackDiagnostic)
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'voice-chat-real')
  if (reason !== null) test.skip(true, reason)
})

test.afterEach(async ({ page, context }, testInfo) => {
  // 本文やIDを含めず、cleanupで失われる失敗理由と完了数を残す。
  const observations = await page.evaluate(() => {
    const state = window.__voiceChatE2E
    if (!state) return null
    return {
      response_tracks: window.__responseTrackDiagnostic?.close(),
      track_media: Object.values(state.trackMediaObservations ?? {}).map(media => ({
        received_at_ms: media.trackReceivedAtMs,
        decoder_missing: media.packetDecodeMissingReason ?? null,
        clock_method: media.workerClockMethod ?? null,
        timeline_interruption: media.packetTimelineInterruption ?? null,
      })),
      core_events: state.coreEventDiagnostics.map(event => ({
        type: event.type, at_ms: event.atMs, reason_code: event.reasonCode,
      })),
      transport_failures: (state.transportFailures ?? []).map(event => ({
        stage: event.stage, reason: event.reason ?? null, at_ms: event.atMs,
      })),
      cycle_count: state.cycles.length,
      first_playback_count: state.cycles.filter(cycle => cycle.startedAt !== null).length,
      complete_playback_count: Object.keys(state.playbackCompletions ?? {}).length,
    }
  })
  await testInfo.attach('voice-state-observations.json', {
    body: JSON.stringify(observations), contentType: 'application/json',
  })
  await context.setOffline(false)
  await driver.endVoiceSession(page)
  await hardDeleteSelectedConversation(page, 'miori')
})

test.use(createVoiceTestUseOptions())
test.describe.configure({ mode: 'serial' })
test.setTimeout(voiceTestTimeout)

const driver = createVoiceChatDriver()

test.describe('通常応答の固定音声', () => {
  test.beforeEach(async ({ page }) => {
    const fixture = parseScheduledFixture(
      readFileSync(new URL('../../playwright/fixtures/speech.wav', import.meta.url)),
      JSON.parse(readFileSync(new URL('../../playwright/fixtures/speech.metadata.json', import.meta.url), 'utf8')),
    )
    await installScheduledFixture(page, fixture)
  })
  test.afterEach(async ({ page }) => {
    await page.evaluate(() => window.__voiceFixtureClock?.close())
  })
  const enableScheduledMicrophone = async (page: Page) => {
    const button = await driver.enableMicrophone(page)
    await expect(page.getByText('セッション: 接続済み')).toBeVisible({ timeout: 15_000 })
    await expect(button).toHaveClass(/mic-standby/)
    await page.evaluate(() => window.__voiceFixtureClock!.start())
    return button
  }

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
  const button = await enableScheduledMicrophone(page)
  await expect(button).toHaveClass(/mic-active/, { timeout: 15_000 })
  await driver.expectMicrophoneStandby(page)
  await expect(button).toHaveAttribute('aria-pressed', 'true')
})

test('実音声応答のuser発話とmiori応答がこの順でチャット欄に表示される', async ({ page }) => {
  await enableScheduledMicrophone(page)
  await driver.expectMessages(page)
  await expect(driver.waitForLiveKitStreamingOrder(page)).resolves.toEqual([
    'text-delta',
    'first-audio-out',
    'completed',
  ])
})

test('通常UIの同一LiveKit sessionで実サービス応答を3往復継続する', async ({ page }) => {
  test.setTimeout(voiceTestTimeout * 3)
  await enableScheduledMicrophone(page)
  for (let count = 1; count <= 3; count += 1) {
    await driver.waitForCompletedVoiceCycles(page, count)
    if (count < 3) {
      await page.waitForFunction(() => window.__voiceFixtureClock?.finished === true)
      await page.evaluate(() => window.__voiceFixtureClock!.replay())
    }
  }

  await expect(page.locator('article.message')).toHaveCount(6)
  await expect(page.getByRole('button', { name: 'マイクをオフにする' }))
    .toHaveAttribute('aria-pressed', 'true')
})

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
