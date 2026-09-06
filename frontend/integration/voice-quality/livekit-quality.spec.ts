import { expect, test } from '@playwright/test'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { PROFILE_REPORT_ENV } from '../../resolved-profile'

import { hardDeleteSelectedConversation } from '../../playwright/conversation-cleanup'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../../playwright/resolved-profile'
import {
  createVoiceChatDriver,
  createVoiceTestUseOptions,
  voiceTestTimeout,
} from '../../playwright/voice-chat-suite'
import {
  normalizeBaselineTranscript,
  validateBaselineFixture,
} from '../../playwright/voice-baseline-fixture'

// pilotは診断用。正式な5 warm-up + 100試行の受入結果とは区別する。
const pilot = process.env.VOICE_QUALITY_PILOT_TRIALS
if (pilot !== undefined && !/^[1-9][0-9]?$/.test(pilot)) {
  throw new Error('VOICE_QUALITY_PILOT_TRIALS must be between 1 and 99')
}
const WARMUP_RUNS = pilot === undefined ? 5 : 1
const MEASURED_RUNS = pilot === undefined ? 100 : Number(pilot)
const fixtureMetadataUrl = new URL(
  '../../playwright/fixtures/speech.metadata.json',
  import.meta.url,
)
const fixtureAudioUrl = new URL('../../playwright/fixtures/speech.wav', import.meta.url)
let resolvedProfile: ResolvedProfile

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({}, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'voice-chat-real')
  if (reason !== null) throw new Error(`real voice services unavailable: ${reason}`)
})

const microphoneOptions = createVoiceTestUseOptions()
test.use({
  ...microphoneOptions,
  launchOptions: {
    args: microphoneOptions.launchOptions.args.map((argument) => (
      argument.startsWith('--use-file-for-fake-audio-capture=')
        ? `${argument}%noloop`
        : argument
    )),
  },
})
test.describe.configure({ mode: 'serial' })
test.setTimeout(voiceTestTimeout * (WARMUP_RUNS + MEASURED_RUNS))

test('LiveKit固定fixtureの独立試行を測定する', async ({ browser }) => {
  const runStartedAt = performance.now()
  const manifestPath = process.env.VOICE_QUALITY_MANIFEST_PATH
  if (manifestPath === undefined) throw new Error('VOICE_QUALITY_MANIFEST_PATH is required')
  const fixture = validateBaselineFixture(
    JSON.parse(await readFile(fixtureMetadataUrl, 'utf8')),
    await readFile(fixtureAudioUrl),
  )
  const expectedTranscript = normalizeBaselineTranscript(fixture.expected_transcript)
  let initialStateHash: string | undefined
  const runFile = promisify(execFile)
  const trials: Record<string, unknown>[] = []

  const persistManifest = async () => {
    await mkdir(dirname(manifestPath), { recursive: true })
    await writeFile(manifestPath, JSON.stringify({
      measurement_scope: pilot === undefined ? "controlled" : "pilot",
      expected_warmup: WARMUP_RUNS, expected_measured: MEASURED_RUNS,
      fixture: {
        fixture_version: fixture.fixture_version,
        audio_sha256: fixture.audio_sha256,
        sample_rate_hz: fixture.sample_rate_hz,
        speech_start_sample: fixture.speech_start_sample,
        speech_end_sample: fixture.speech_end_sample,
      },
      initial_state_hash: initialStateHash, trials,
    }, null, 2))
  }

  for (let index = 0; index < WARMUP_RUNS + MEASURED_RUNS; index += 1) {
    const page = await browser.newPage({
      baseURL: 'http://localhost:5173', permissions: ['microphone'],
    })
    const driver = createVoiceChatDriver()
    try {
      const microphone = await driver.openVoiceChat(page)
      const conversationId = await page.evaluate(() => (
        localStorage.getItem('digital-souls:conversation:miori')
      ))
      const dataRoot = process.env.DS_DATA_DIR
      const profilePath = process.env[PROFILE_REPORT_ENV]
      if (!conversationId || !dataRoot || !profilePath) throw new Error('initial state evidence is unavailable')
      const repositoryRoot = resolve('..')
      const stateResult = await runFile(resolve(repositoryRoot, 'backend/.venv/bin/python'), [
        '-m', 'app.voice_quality_state', '--data-root', dataRoot, '--profile', profilePath,
        '--repository-root', repositoryRoot, '--conversation-id', conversationId,
      ], { cwd: repositoryRoot, env: { ...process.env, PYTHONPATH: resolve(repositoryRoot, 'backend') } })
      const state: { initial_state_hash: string; evidence: Record<string, unknown> } = JSON.parse(stateResult.stdout)
      initialStateHash ??= state.initial_state_hash
      if (state.initial_state_hash !== initialStateHash) throw new Error('controlled initial state changed between trials')
      await microphone.click()
      await expect(microphone).toHaveAttribute('aria-pressed', 'true')
      await driver.waitForSpeechCompletion(page)
      await page.waitForFunction(() => window.__voiceChatE2E.cycles.some((cycle) => (
        cycle.startedAt !== null && cycle.audioReceivedAt !== null && cycle.audioDecodeAt !== null
      )), undefined, { timeout: voiceTestTimeout })
      const cycle = await driver.waitForCompletedVoiceCycle(page)
      await expect(page.locator('article.message')).toHaveCount(2)
      const transcriptMatches = normalizeBaselineTranscript(
        await driver.readUserTranscript(page),
      ) === expectedTranscript
      if (!transcriptMatches) throw new Error('controlled baseline transcript did not match fixture')
      await page.waitForFunction((responseId) => (
        window.__voiceChatE2E.liveKitOrder.includes(`${responseId}:completed`)
      ), cycle.responseId, { timeout: voiceTestTimeout })
      // page.closeだけでは再接続猶予中のroomが残る。明示終了の完了後に次試行へ進む。
      const sessionEnded = page.waitForResponse((response) => (
        response.request().method() === 'DELETE'
        && new URL(response.url()).pathname.endsWith(`/voice/livekit/sessions/${cycle.sessionId}`)
      ))
      await driver.endVoiceSession(page)
      const endResponse = await sessionEnded
      expect(endResponse.ok()).toBe(true)
      expect((await endResponse.json()).phase).toBe('ended')
      trials.push({
        session_end_confirmed: true,
        phase: index < WARMUP_RUNS ? 'warmup' : 'measured',
        outcome: 'success',
        first_playback_method: 'audio_worklet_output_timestamp',
        media_observation_method: 'rtc_encoded_transform_and_decoded_track_first_response',
        fixture_version: fixture.fixture_version,
        audio_sha256: fixture.audio_sha256,
        transcript_matches: transcriptMatches,
        initial_state_hash: initialStateHash,
        initial_state_evidence: state.evidence,
        fixture_speech_end_client_ms: cycle.fixtureStartedAt
          + fixture.speech_end_sample * 1000 / fixture.sample_rate_hz,
        ...cycle,
      })
      await hardDeleteSelectedConversation(page, 'miori')
    } catch (error) {
      const diagnostics = await page.evaluate(() => ({
        cycles: window.__voiceChatE2E.cycles,
        coreEvents: window.__voiceChatE2E.coreEventDiagnostics,
        liveKitOrder: window.__voiceChatE2E.liveKitOrder,
        microphoneStates: window.__voiceChatE2E.micStates,
      })).catch(() => ({ unavailable: true }))
      trials[index] = {
        ...trials[index],
        phase: index < WARMUP_RUNS ? 'warmup' : 'measured',
        outcome: 'failure', failure_reason: 'voice_cycle_incomplete', diagnostics,
      }
      throw error
    } finally {
      await persistManifest()
      await page.close()
    }
  }

  const resourceUsage = process.resourceUsage()
  const elapsedMicroseconds = (performance.now() - runStartedAt) * 1000
  await mkdir(dirname(manifestPath), { recursive: true })
  await writeFile(manifestPath, JSON.stringify({
    measurement_scope: pilot === undefined ? 'controlled' : 'pilot',
    expected_warmup: WARMUP_RUNS,
    expected_measured: MEASURED_RUNS,
    fixture: {
      fixture_version: fixture.fixture_version,
      audio_sha256: fixture.audio_sha256,
      sample_rate_hz: fixture.sample_rate_hz,
      speech_start_sample: fixture.speech_start_sample,
      speech_end_sample: fixture.speech_end_sample,
    },
    initial_state_hash: initialStateHash,
    trials,
    diagnostics: {
      cpu_percent: resourceUsage.userCPUTime * 100 / elapsedMicroseconds,
      maximum_resident_set_bytes: resourceUsage.maxRSS * 1024,
    },
  }, null, 2))
})
