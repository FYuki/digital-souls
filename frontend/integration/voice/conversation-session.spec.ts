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
  const resolvedProfile = await readResolvedProfile()
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'voice-chat-real')
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

const waitForTextPlayback = async (page: Page, previous: string[] = []) => {
  const result = await page.waitForFunction(ignored => {
    const state = window.__voiceChatE2E
    const event = state.coreEventDiagnostics.find(event => event.type === 'response_started'
      && typeof event.responseId === 'string' && !ignored.includes(event.responseId)
      && state.responseSourceUtterances?.[event.responseId]?.length === 0)
    if (typeof event?.responseId !== 'string') return null
    const playback = state.playbackCompletions?.[event.responseId]
    return playback ? {responseId: event.responseId, ...playback} : null
  }, previous, {timeout: 90_000})
  const playback = await result.jsonValue()
  if (playback === null) throw new Error('テキスト回答の再生完了を観測できない')
  expect(playback.renderedSamples).toBeGreaterThan(0)
  expect(playback.packetCount).toBeGreaterThan(0)
  return playback
}

test('focus中の実音声を取り込まず、Enter受理後の次の音声を同じsessionで取り込む', async ({page}, testInfo) => {
  await driver.enableMicrophone(page)
  const input = page.getByLabel('メッセージ')
  await input.fill('こんにちは。短く挨拶してください。')
  await expect(page.getByText('入力: テキスト入力中')).toBeVisible()
  await page.evaluate(() => window.__voiceFixtureClock!.start())
  await page.waitForFunction(() => window.__voiceFixtureClock?.finished === true)
  // 固定音声の全区間を流しても、focus中はCoreへの音声入力を確定しない。
  expect(await page.evaluate(() => window.__voiceChatE2E.cycles)).toHaveLength(0)
  expect(await page.evaluate(() => window.__voiceChatE2E.coreEventDiagnostics
    .filter(event => event.type === 'response_started'))).toHaveLength(0)
  await input.press('Enter')
  await expect(input).toHaveValue('', {timeout: 60_000})
  await expect(input).not.toBeFocused()
  await expect(page.getByText('入力: 聞き取り中')).toBeVisible()
  const playback = await waitForTextPlayback(page)
  await page.evaluate(() => window.__voiceFixtureClock!.replay())
  await driver.waitForCompletedVoiceCycles(page, 1)
  const conversationId = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:miori'))
  await expect.poll(async () => (await history(page, conversationId!)).length).toBe(2)
  await testInfo.attach('focus-suppression-evidence.json', {body: JSON.stringify({
    suppressedFixtureCycles: 0, resumedVoiceCycles: 1, historyTurns: 2,
    textPlaybackSamples: playback.renderedSamples,
  }), contentType: 'application/json'})
})

for (const manualMute of [false, true]) {
 for (const operation of ['click', 'Enter'] as const) {
  test(`${operation}受理後の手動ミュート=${manualMute}を保ち、focusだけでは実TTSを止めない`, async ({page}, testInfo) => {
    const microphone = await driver.enableMicrophone(page)
    if (manualMute) await microphone.click()
    const input = page.getByLabel('メッセージ')
    await input.fill('夜空の星を眺める楽しさを、三文で説明してください。')
    if (operation === 'Enter') await input.press('Enter')
    else await page.getByRole('button', {name: '送信', exact: true}).click()
    await expect(input).toHaveValue('', {timeout: 60_000})
    await expect(input).not.toBeFocused()
    await expect(page.getByRole('button', {name: /マイクを(オン|オフ)にする/}))
      .toHaveAttribute('aria-pressed', String(!manualMute))
    const active = await page.waitForFunction(() => {
      const state = window.__voiceChatE2E
      const id = state.activeResponseId
      return id && state.liveKitOrder.includes(`${id}:rendered-audio`)
        && !state.playbackCompletions?.[id] ? id : null
    }, undefined, {timeout: 60_000})
    const responseId = await active.jsonValue()
    expect(typeof responseId).toBe('string')
    await input.focus()
    await expect(input).toBeFocused()
    const playback = await waitForTextPlayback(page)
    expect(playback.responseId).toBe(responseId)
    expect(await page.evaluate(id => window.__voiceChatE2E.coreEventDiagnostics.some(event =>
      event.responseId === id && event.type === 'response_cancelled'), responseId)).toBe(false)
    await expect(page.getByRole('button', {name: /マイクを(オン|オフ)にする/}))
      .toHaveAttribute('aria-pressed', String(!manualMute))
    await testInfo.attach('focus-playback-evidence.json', {body: JSON.stringify({
      operation, manualMute, focusDuringPlayback: true, responseCancelled: false,
      completedSamples: playback.renderedSamples, completedPackets: playback.packetCount,
    }), contentType: 'application/json'})
  })
 }
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
