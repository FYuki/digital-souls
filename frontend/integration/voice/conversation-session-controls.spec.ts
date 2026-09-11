import {readFileSync} from 'node:fs'
import {expect, test, type Page} from '@playwright/test'
import {installScheduledFixture, parseScheduledFixture} from '../../playwright/controlled-audio-fixture'
import {attachProfileEvidence, getCapabilitySkipReason, readResolvedProfile} from '../../playwright/resolved-profile'
import {createVoiceChatDriver, createVoiceTestUseOptions, voiceTestTimeout} from '../../playwright/voice-chat-suite'

const driver = createVoiceChatDriver()
const owned = new WeakMap<Page, Set<string>>()
test.use(createVoiceTestUseOptions())
test.describe.configure({mode: 'serial'})
test.setTimeout(voiceTestTimeout * 3)

test.beforeEach(async ({page}, testInfo) => {
  owned.set(page, new Set())
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
  const evidence = await page.evaluate(() => ({
    source: 'typed_text_with_real_voice_session',
    coreEvents: window.__voiceChatE2E?.coreEventDiagnostics,
    playback: window.__voiceChatE2E?.playbackCompletions,
    transportFailures: window.__voiceChatE2E?.transportFailures ?? [],
  }))
  await testInfo.attach('conversation-controls-real.json', {
    body: JSON.stringify(evidence), contentType: 'application/json',
  })
  try { await driver.endVoiceSession(page) } finally {
    try { await page.evaluate(() => window.__voiceFixtureClock?.close()) } finally {
      for (const id of owned.get(page) ?? []) {
        const response = await page.request.delete(`/api/characters/miori/conversations/${id}`)
        expect(response.status()).toBe(204)
      }
    }
  }
})

const rememberConversation = async (page: Page) => {
  const id = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:miori'))
  if (!id) throw new Error('試験用Conversationがない')
  owned.get(page)!.add(id)
  return id
}

const waitForResponse = async (page: Page, previous: string[] = []) => {
  const handle = await page.waitForFunction(ignored =>
    window.__voiceChatE2E.coreEventDiagnostics.find(event => event.type === 'response_started'
      && typeof event.responseId === 'string' && !ignored.includes(event.responseId))?.responseId ?? null,
  previous, {timeout: 60_000})
  const id = await handle.jsonValue()
  if (typeof id !== 'string') throw new Error('回答開始を観測できない')
  return id
}

const waitForPlayback = async (page: Page, responseId: string) => {
  const handle = await page.waitForFunction(id => window.__voiceChatE2E.playbackCompletions?.[id] ?? null,
    responseId, {timeout: 90_000})
  const result = await handle.jsonValue()
  if (!result) throw new Error('回答再生の完了を観測できない')
  expect(result.renderedSamples).toBeGreaterThan(0)
  return result
}

const observeResponse = (page: Page, responseId: string) => page.evaluate(id => ({
  terminal: window.__voiceChatE2E.coreEventDiagnostics.filter(event => event.responseId === id
    && ['response_completed', 'response_cancelled', 'response_failed', 'response_privacy_skipped'].includes(String(event.type)))
    .map(event => event.type),
  renderedAudio: window.__voiceChatE2E.liveKitOrder.includes(`${id}:rendered-audio`),
  playbackCompleted: window.__voiceChatE2E.playbackCompletions?.[id] !== undefined,
}), responseId)

for (const phase of ['generation', 'playback'] as const) {
  test(`実回答の${phase}中にtextで割り込み、新回答を同じsessionで再生する`, async ({page}, testInfo) => {
    await driver.enableMicrophone(page)
    await rememberConversation(page)
    const input = page.getByLabel('メッセージ')
    await input.fill('星空を眺める楽しさについて、十文でゆっくり詳しく説明してください。')
    await input.press('Enter')
    const previous = await waitForResponse(page)
    await expect(input).toHaveValue('', {timeout: 60_000})
    if (phase === 'playback') {
      await page.waitForFunction(id => window.__voiceChatE2E.liveKitOrder.includes(`${id}:rendered-audio`),
        previous, {timeout: 60_000})
      expect(await page.evaluate(id => window.__voiceChatE2E.playbackCompletions?.[id] !== undefined, previous)).toBe(false)
    } else {
      expect(await observeResponse(page, previous)).toEqual({terminal: [], renderedAudio: false, playbackCompleted: false})
    }
    await input.fill('説明を止めて、こんにちはとだけ挨拶してください。')
    // 入力操作中にphaseが進んだ場合も、別phaseの試験成功として記録しない。
    const beforeSubmit = await observeResponse(page, previous)
    if (phase === 'generation') expect(beforeSubmit).toEqual({terminal: [], renderedAudio: false, playbackCompleted: false})
    else expect(beforeSubmit).toMatchObject({renderedAudio: true, playbackCompleted: false})
    await input.press('Enter')
    const next = await waitForResponse(page, [previous])
    expect(next).not.toBe(previous)
    const playback = await waitForPlayback(page, next)
    const state = await page.evaluate(old => ({
      terminal: window.__voiceChatE2E.coreEventDiagnostics.filter(event => event.responseId === old
        && ['response_completed', 'response_cancelled', 'response_failed'].includes(String(event.type))).map(event => event.type),
      oldPlaybackCompleted: window.__voiceChatE2E.playbackCompletions?.[old] !== undefined,
      sessions: [...new Set(window.__voiceChatE2E.coreEventDiagnostics.map(event => event.sessionId).filter(Boolean))],
    }), previous)
    expect(state.terminal).toHaveLength(1)
    // 生成完了とcancelが競合する場合も、BEの終端を一つだけ維持する。
    expect(['response_completed', 'response_cancelled']).toContain(state.terminal[0])
    expect(state.oldPlaybackCompleted).toBe(false)
    expect(state.sessions).toHaveLength(1)
    await testInfo.attach('text-interruption-evidence.json', {body: JSON.stringify({
      phase, previous, next, beforeSubmit, ...state, newPlaybackSamples: playback.renderedSamples,
    }), contentType: 'application/json'})
  })
}

test('実音声session Aを維持してBへ通常textを送り、A復帰後の送信でも切替ミュートを保つ', async ({page}, testInfo) => {
  const chatRequests: Array<Record<string, string>> = []
  let tokens = 0
  page.on('request', request => {
    if (request.url().endsWith('/api/chat')) chatRequests.push(request.postDataJSON())
    if (request.url().endsWith('/api/voice/livekit/token')) tokens += 1
  })
  await driver.enableMicrophone(page)
  const a = await rememberConversation(page)
  const input = page.getByLabel('メッセージ')
  await input.fill('夜空の星の美しさについて、五文で説明してください。')
  await input.press('Enter')
  const responseA = await waitForResponse(page)
  await expect(input).toHaveValue('', {timeout: 60_000})
  await page.getByRole('button', {name: '新規スレッド（光織）'}).click()
  await expect.poll(() => page.evaluate(() => localStorage.getItem('digital-souls:conversation:miori'))).not.toBe(a)
  const b = await rememberConversation(page)
  expect(b).not.toBe(a)
  await input.fill('こんにちは。短く挨拶してください。')
  await page.getByRole('button', {name: '送信', exact: true}).click()
  await expect(input).toHaveValue('', {timeout: 90_000})
  expect(chatRequests).toHaveLength(1)
  expect(chatRequests[0].conversation_id).toBe(b)
  await waitForPlayback(page, responseA)
  expect(await page.evaluate(id => window.__voiceChatE2E.coreEventDiagnostics.some(event =>
    event.responseId === id && event.type === 'response_cancelled'), responseA)).toBe(false)
  for (const id of [a, b]) {
    const result = await page.request.get(`/api/characters/miori/conversations/${id}/turns`)
    expect(result.ok()).toBe(true)
    expect(await result.json()).toHaveLength(1)
  }
  await page.locator('.thread-row').filter({has: page.locator(`[data-thread-menu="miori-${a}"]`)})
    .locator('.thread-select').click()
  await expect(page.getByRole('button', {name: 'マイクをオンにする'})).toHaveAttribute('aria-pressed', 'false')
  await input.fill('ありがとう。短く返事をしてください。')
  await input.press('Enter')
  await expect(input).toHaveValue('', {timeout: 60_000})
  await expect(input).not.toBeFocused()
  await expect(page.getByRole('button', {name: 'マイクをオンにする'})).toHaveAttribute('aria-pressed', 'false')
  const next = await waitForResponse(page, [responseA])
  await waitForPlayback(page, next)
  expect(chatRequests).toHaveLength(1)
  expect(tokens).toBe(1)
  await testInfo.attach('thread-routing-evidence.json', {body: JSON.stringify({
    separateHistories: true, ordinaryChatRequests: 1, voiceTokenRequests: tokens,
    originalResponseCompleted: true, switchMuteRetained: true,
  }), contentType: 'application/json'})
})
