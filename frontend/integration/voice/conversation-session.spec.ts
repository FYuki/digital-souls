import {readFileSync} from 'node:fs'
import {expect, test, type Page} from '@playwright/test'
import {installScheduledFixture, parseScheduledFixture} from '../../playwright/controlled-audio-fixture'
import {hardDeleteSelectedConversation} from '../../playwright/conversation-cleanup'
import {attachProfileEvidence, getCapabilitySkipReason, readResolvedProfile} from '../../playwright/resolved-profile'
import {createVoiceChatDriver, createVoiceTestUseOptions, voiceTestTimeout} from '../../playwright/voice-chat-suite'

const driver = createVoiceChatDriver()
test.use(createVoiceTestUseOptions())
test.describe.configure({mode: 'serial'})
test.setTimeout(voiceTestTimeout * 3)

test.beforeEach(async ({page}, testInfo) => {
  const profile = await readResolvedProfile()
  await attachProfileEvidence(testInfo, profile)
  const reason = getCapabilitySkipReason(profile, 'voice-chat-real')
  if (reason !== null) test.skip(true, reason)
  await installScheduledFixture(page, parseScheduledFixture(
    readFileSync(new URL('../../playwright/fixtures/speech.wav', import.meta.url)),
    JSON.parse(readFileSync(new URL('../../playwright/fixtures/speech.metadata.json', import.meta.url), 'utf8')),
  ))
})

test.afterEach(async ({page}, testInfo) => {
  if (page.url() === 'about:blank') return
  const evidence = await page.evaluate(() => {
    const state = window.__voiceChatE2E
    return {
      source: 'fixed_audio_fixture_and_typed_text',
      coreEvents: state?.coreEventDiagnostics,
      playback: state?.playbackCompletions,
      transportFailures: state?.transportFailures ?? [],
    }
  })
  await testInfo.attach('conversation-session-real.json', {
    body: JSON.stringify(evidence, null, 2), contentType: 'application/json',
  })
  try {
    await driver.endVoiceSession(page)
  } finally {
    await page.evaluate(() => window.__voiceFixtureClock?.close())
    await hardDeleteSelectedConversation(page, 'miori')
  }
})

type HistoryTurn = {turn_id: string; user_content: string; assistant_content: string}
const history = async (page: Page, conversationId: string): Promise<HistoryTurn[]> => {
  const response = await page.request.get(`/api/characters/miori/conversations/${conversationId}/turns`)
  expect(response.ok()).toBe(true)
  return response.json()
}

test('同じ実Conversationで音声→text→音声を保存し、textにもTTSを再生する', async ({page}, testInfo) => {
  const tokenRequests: string[] = []
  page.on('request', request => {
    if (request.url().endsWith('/api/voice/livekit/token')) tokenRequests.push(request.url())
  })
  await driver.enableMicrophone(page)
  const conversationId = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:miori'))
  expect(conversationId).not.toBeNull()
  await page.evaluate(() => window.__voiceFixtureClock!.start())
  await driver.waitForCompletedVoiceCycles(page, 1)
  const before = await history(page, conversationId!)
  expect(before).toHaveLength(1)

  const input = page.getByLabel('メッセージ')
  const typedText = 'ここまでの会話の話題を、一文で簡潔に説明してください。'
  await input.fill(typedText)
  await expect(page.getByText('入力: テキスト入力中')).toBeVisible()
  await input.press('Enter')
  await expect(input).toHaveValue('', {timeout: 60_000})
  await expect(input).not.toBeFocused()
  const responseId = await page.evaluate(() =>
    window.__voiceChatE2E.coreEventDiagnostics.filter(event => event.type === 'response_started').at(-1)?.responseId,
  )
  expect(typeof responseId).toBe('string')
  const textPlayback = await page.waitForFunction(id => {
    const state = window.__voiceChatE2E
    if (state.coreEventDiagnostics.some(event => event.responseId === id && event.type === 'response_failed')) {
      throw new Error('text response failed')
    }
    return state.playbackCompletions?.[String(id)] ?? null
  }, responseId, {timeout: 90_000})
  const playback = await textPlayback.jsonValue()
  if (playback === null) throw new Error('text playback did not complete')
  expect(playback.renderedSamples).toBeGreaterThan(0)
  expect(playback.packetCount).toBeGreaterThan(0)
  expect(await page.evaluate(id => window.__voiceChatE2E.responseSourceUtterances?.[String(id)], responseId)).toEqual([])
  await expect.poll(async () => (await history(page, conversationId!)).length, {timeout: 30_000}).toBe(2)
  const afterText = await history(page, conversationId!)
  expect(afterText.some(turn => turn.turn_id === before[0].turn_id)).toBe(true)
  const typedTurn = afterText.find(turn => turn.user_content === typedText)
  expect(typedTurn?.assistant_content.trim().length).toBeGreaterThan(0)

  await page.waitForFunction(() => window.__voiceFixtureClock?.finished === true)
  await page.evaluate(() => window.__voiceFixtureClock!.replay())
  await driver.waitForCompletedVoiceCycles(page, 2)
  await expect.poll(async () => (await history(page, conversationId!)).length, {timeout: 30_000}).toBe(3)
  const final = await history(page, conversationId!)
  expect(new Set(final.map(turn => turn.turn_id)).size).toBe(3)
  expect(final.filter(turn => turn.user_content === typedText)).toHaveLength(1)
  expect(final.some(turn => turn.turn_id === before[0].turn_id)).toBe(true)
  await expect(page.locator('article.message')).toHaveCount(6)
  expect(tokenRequests).toHaveLength(1)
  expect(await page.evaluate(() => new Set(window.__voiceChatE2E.coreEventDiagnostics
    .map(event => event.sessionId).filter(Boolean)).size)).toBe(1)
  await testInfo.attach('mixed-history-evidence.json', {body: JSON.stringify({
    historyCounts: [before.length, afterText.length, final.length],
    retainedFirstTurn: true, uniqueTurns: 3, tokenRequests: tokenRequests.length,
    textPlaybackSamples: playback.renderedSamples, textPlaybackPackets: playback.packetCount,
  }, null, 2), contentType: 'application/json'})
})
