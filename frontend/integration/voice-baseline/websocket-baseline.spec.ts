import { expect, test } from '@playwright/test'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname } from 'node:path'
import { createHash } from 'node:crypto'

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

const WARMUP_RUNS = 5
const MEASURED_RUNS = 100
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
  if (reason !== null) test.skip(true, reason)
})

test.use(createVoiceTestUseOptions())
test.describe.configure({ mode: 'serial' })
test.setTimeout(voiceTestTimeout * (WARMUP_RUNS + MEASURED_RUNS))

test('固定fixtureで5 warm-upと100独立試行を実行する', async ({ browser }) => {
  const runStartedAt = performance.now()
  const manifestPath = process.env.VOICE_BASELINE_MANIFEST_PATH
  if (manifestPath === undefined) throw new Error('VOICE_BASELINE_MANIFEST_PATH is required')
  const fixture = validateBaselineFixture(
    JSON.parse(await readFile(fixtureMetadataUrl, 'utf8')),
    await readFile(fixtureAudioUrl),
  )
  const expectedTranscript = normalizeBaselineTranscript(fixture.expected_transcript)
  const initialStateHash = createHash('sha256')
    .update('miori:new-conversation:empty-history:v1')
    .digest('hex')
  const trials: Record<string, unknown>[] = []

  for (let index = 0; index < WARMUP_RUNS + MEASURED_RUNS; index += 1) {
    const page = await browser.newPage()
    const driver = createVoiceChatDriver()
    try {
      await driver.enableMicrophone(page)
      await driver.waitForSpeechCompletion(page)
      const cycle = await driver.waitForCompletedVoiceCycle(page)
      await expect(page.locator('article.message')).toHaveCount(2)
      const transcriptMatches = normalizeBaselineTranscript(
        await driver.readUserTranscript(page),
      ) === expectedTranscript
      if (!transcriptMatches) throw new Error('controlled baseline transcript did not match fixture')
      trials.push({
        phase: index < WARMUP_RUNS ? 'warmup' : 'measured',
        fixture_version: fixture.fixture_version,
        audio_sha256: fixture.audio_sha256,
        transcript_matches: transcriptMatches,
        initial_state_hash: initialStateHash,
        fixture_speech_end_client_ms: cycle.fixtureStartedAt
          + fixture.speech_end_sample * 1000 / fixture.sample_rate_hz,
        ...cycle,
      })
      await hardDeleteSelectedConversation(page, 'miori')
    } finally {
      await page.close()
    }
  }

  const resourceUsage = process.resourceUsage()
  const elapsedMicroseconds = (performance.now() - runStartedAt) * 1000
  await mkdir(dirname(manifestPath), { recursive: true })
  await writeFile(manifestPath, JSON.stringify({
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
