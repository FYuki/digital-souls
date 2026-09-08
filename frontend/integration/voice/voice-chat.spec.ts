import { installResponseTrackDiagnostic } from '../../playwright/response-track-readiness-diagnostic'
import { readFileSync } from 'node:fs'
import { installScheduledFixture, parseScheduledFixture, readFixtureBounds } from '../../playwright/controlled-audio-fixture'
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

test.afterEach(async ({ page }, testInfo) => {
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
        decision: event.decision, final: event.final,
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
  await page.evaluate(() => window.__voiceFixtureClock?.close())
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

test('ラベル付き実音声によるLiveKit barge-inのlocal停止とcancel確定latencyを記録する', async ({ page }, testInfo) => {
  test.setTimeout(voiceTestTimeout * 2)
  const initial = parseScheduledFixture(
    readFileSync(new URL('../../playwright/fixtures/speech.wav', import.meta.url)),
    JSON.parse(readFileSync(new URL('../../playwright/fixtures/speech.metadata.json', import.meta.url), 'utf8')),
  )
  const catalog = JSON.parse(readFileSync(new URL('../../playwright/fixtures/voice-quality-v2/manifest.json', import.meta.url), 'utf8')) as {
    sources: Array<{id: string; cohort: string; source_file: string; source_sha256: string;
      speech_start_sample: number; speech_end_sample: number}>
  }
  const source = catalog.sources.find(item => item.id === 'take-01' && item.cohort === 'take_turn')
  if (!source) throw new Error('labeled take-turn fixture unavailable')
  const interruption = parseScheduledFixture(
    readFileSync(new URL(`../../playwright/fixtures/voice-quality-v2/${source.source_file}`, import.meta.url)),
    {audio_sha256: source.source_sha256, sample_rate_hz: 48000,
      speech_start_sample: source.speech_start_sample, speech_end_sample: source.speech_end_sample},
  )
  await installScheduledFixture(page, initial)
  await driver.enableMicrophone(page)
  await expect(page.getByText('セッション: 接続済み')).toBeVisible({timeout: 15_000})
  await page.evaluate(() => window.__voiceFixtureClock!.start())
  const cycle = await driver.waitForCompletedVoiceCycle(page)
  await page.waitForFunction(() => window.__voiceFixtureClock?.finished === true)
  // 初回応答の実再生中にPCMを流し、通常のVAD→STT→意図判定を通す。
  expect(await page.evaluate(responseId => window.__voiceChatE2E.activeResponseId === responseId
    && (window.__voiceChatE2E.activeAudioGraphs ?? 0) > 0
    && !window.__voiceChatE2E.playbackCompletions?.[responseId], cycle.responseId)).toBe(true)
  await page.evaluate(fixture => window.__voiceFixtureClock!.replay(fixture), interruption)
  await page.waitForFunction(() => window.__voiceFixtureClock?.finished === true)
  const bounds = await readFixtureBounds(page)

  const evidence = await driver.waitForInterruptionEvidence(page) as {
    responseId: string
    speechStartedAtMs: number
    localPlaybackStoppedAtMs: number
    cancelConfirmedAtMs: number
  }
  expect(evidence.responseId).toBe(cycle.responseId)
  await page.waitForFunction(responseId => window.__voiceChatE2E.coreEventDiagnostics.some(event =>
    event.type === 'turn_decision' && event.responseId === responseId && event.final === true),
  cycle.responseId, {timeout: 10_000})
  expect(await page.evaluate(responseId => window.__voiceChatE2E.coreEventDiagnostics.find(event =>
    event.type === 'turn_decision' && event.responseId === responseId && event.final === true)?.decision,
  cycle.responseId)).toBe('take_turn')
  // 正解の実音声開始からの上限も評価し、turn判定受信時刻で起点を置き換えない。
  const localStopFromFixtureUpperMs = evidence.localPlaybackStoppedAtMs - bounds.speechStart.lowerMs
  const cancelFromFixtureUpperMs = evidence.cancelConfirmedAtMs - bounds.speechStart.lowerMs
  expect(localStopFromFixtureUpperMs).toBeGreaterThanOrEqual(0)
  expect(localStopFromFixtureUpperMs).toBeLessThanOrEqual(3_000)
  expect(cancelFromFixtureUpperMs).toBeGreaterThanOrEqual(0)
  expect(cancelFromFixtureUpperMs).toBeLessThanOrEqual(3_500)
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
      fixture_sha256: interruption.audioSha256,
      fixture_clock_bounds: bounds,
      localStopFromFixtureUpperMs,
      cancelFromFixtureUpperMs,
      response_correlation_verified: true,
    }, null, 2),
    contentType: 'application/json',
  })
})

// 再接続はtest:integration:voice:reconnectで専用bridgeへ実障害を入れて検証する。
// livekit-quality.spec.tsのcontrol-probe分岐が、control/audio復旧・同sessionでの次応答・完全再生を確認する。
