// 実サービスで同一sessionのtrack切替を確認する。独立100試行の代用にはしない。
import { expect, type Browser } from '@playwright/test'
import { writeFile, mkdir } from 'node:fs/promises'
import { dirname } from 'node:path'
import { installScheduledFixture, readFixtureBounds, type ScheduledFixture } from './controlled-audio-fixture'
import { createVoiceChatDriver, voiceTestTimeout } from './voice-chat-suite'
import { normalizeBaselineTranscript } from './voice-baseline-fixture'

export const measureResponseTrackSession = async (
  browser: Browser, fixture: ScheduledFixture, expectedTranscript: string, turns: number, output: string,
): Promise<void> => {
  const page = await browser.newPage({ baseURL: 'http://localhost:5173', permissions: ['microphone'] })
  const driver = createVoiceChatDriver()
  const observations: Record<string, unknown>[] = []
  let ended = false
  let failureDiagnostics: Record<string, unknown> | null = null
  const persist = async () => {
    await mkdir(dirname(output), { recursive: true })
    await writeFile(output, JSON.stringify({
      measurement_scope: 'continuous_response_track_diagnostic', expected_turns: turns,
      fixture_sha256: fixture.audioSha256, session_end_confirmed: ended, trials: observations,
      failure_diagnostics: failureDiagnostics,
    }, null, 2) + '\n')
  }
  try {
    await installScheduledFixture(page, fixture)
    const microphone = await driver.openVoiceChat(page)
    await microphone.click()
    await expect(microphone).toHaveAttribute('aria-pressed', 'true')
    for (let index = 0; index < turns; index++) {
      await page.evaluate(async replay => {
        if (replay) await window.__voiceFixtureClock!.replay()
        else await window.__voiceFixtureClock!.start()
      }, index > 0)
      const cycles = await driver.waitForCompletedVoiceCycles(page, index + 1)
      if (cycles === null) throw new Error('completed cycles are unavailable')
      const cycle = cycles[index]
      await page.waitForFunction(responseId => {
        if (window.__voiceChatE2E.transportFailures?.length) throw new Error('voice transport failed')
        if (window.__voiceChatE2E.coreEventDiagnostics.some(event => event.type === 'response_failed')) throw new Error('voice response failed')
        return window.__voiceChatE2E.liveKitOrder.includes(`${responseId}:completed`)
      },
        cycle.responseId, { timeout: voiceTestTimeout })
      await page.waitForFunction(responseId => !!window.__voiceChatE2E.playbackCompletions?.[responseId], cycle.responseId!, {timeout: 10_000})
      await page.waitForFunction(responseId => !!window.__voiceChatE2E.networkObservations?.[responseId], cycle.responseId!, {timeout: 3000})
      const transcript = await page.locator('article.message').nth(index * 2).locator('p').textContent()
      expect(normalizeBaselineTranscript(transcript ?? '')).toBe(expectedTranscript)
      expect(cycle.sessionId).toBe(cycles[0].sessionId)
      expect(cycle.conversationId).toBe(cycles[0].conversationId)
      expect(new Set(cycles.map(value => value.responseId)).size).toBe(index + 1)
      const trackMatches = await page.evaluate(responseId => window.__voiceChatE2E.lastTrackMediaResponseId === responseId, cycle.responseId)
      expect(trackMatches).toBe(true)
      await expect(microphone).toHaveAttribute('aria-pressed', 'true')
      observations.push({ ...cycle, outcome: 'success', transcript_matches: true,
        track_response_matches: trackMatches, additional_user_control_actions: 0,
        packet_playback_observation: await page.evaluate(() => window.__voiceChatE2E.lastPacketPlaybackObservation),
        track_media_observation: await page.evaluate(() => window.__voiceChatE2E.lastTrackMediaObservation),
        media_observation_method: 'response_track_stateful_opus_worklet_output',
        network_observation: await page.evaluate(responseId => window.__voiceChatE2E.networkObservations?.[responseId], cycle.responseId!),
        playback_completion: await page.evaluate(responseId => window.__voiceChatE2E.playbackCompletions?.[responseId], cycle.responseId!),
        fixture_clock_bounds: await readFixtureBounds(page) })
      await persist()
      await page.waitForFunction(() => window.__voiceFixtureClock?.finished, undefined, { timeout: voiceTestTimeout })
    }
    const sessionId = observations[0].sessionId
    const request = page.waitForResponse(response => response.request().method() === 'DELETE'
      && new URL(response.url()).pathname.endsWith(`/voice/livekit/sessions/${sessionId}`))
    await driver.endVoiceSession(page)
    const response = await request
    expect(response.ok()).toBe(true)
    expect((await response.json()).phase).toBe('ended')
    ended = true
    await persist()
  } catch (error) {
    // 失敗した発話も本文なしで保存し、応答なしと再生待ちを区別する。
    failureDiagnostics = await page.evaluate(() => ({
      core_events: window.__voiceChatE2E.coreEventDiagnostics,
      transport_failures: window.__voiceChatE2E.transportFailures ?? [],
      media_observation: window.__voiceChatE2E.lastTrackMediaObservation ?? null,
      cycles: window.__voiceChatE2E.cycles,
      microphone_states: window.__voiceChatE2E.micStates,
      playback_completions: window.__voiceChatE2E.playbackCompletions ?? {},
      network_observations: window.__voiceChatE2E.networkObservations ?? {},
      fixture_finished: window.__voiceFixtureClock?.finished ?? false,
    })).catch(() => ({ browser_state_unavailable: true }))
    throw error
  } finally {
    if (!ended) {
      await persist()
      await driver.endVoiceSession(page).catch(() => undefined)
    }
    await page.evaluate(() => window.__voiceFixtureClock?.close()).catch(() => undefined)
    await page.close()
  }
}
