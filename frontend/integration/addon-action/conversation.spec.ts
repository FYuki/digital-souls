import { expect, test, type Page } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import { createVoiceChatDriver } from '../../playwright/voice-chat-suite'

// backend・MCP・STT・LLM・TTS・LiveKitの応答を差し替えない。
const driver = createVoiceChatDriver()
const sample = process.env.TOOL_USE_TEST_SAMPLE!
const sampleText = async () => (await readFile(sample, 'utf8')).trim()
const confirmation = (page: Page) => page.getByRole('region', { name: '外部操作の承認', exact: true })

test.afterEach(async ({ page }, info) => {
  if (info.status !== info.expectedStatus) {
    // 破棄可能な検証会話と数値イベントだけを残す。通信payloadは記録しない。
    await info.attach('synthetic-conversation', {
      body: JSON.stringify(await page.locator('article.message').allTextContents()), contentType: 'application/json',
    })
    await info.attach('voice-progress', {
      body: JSON.stringify(await page.evaluate(() => {
        const p = window.__voiceChatE2E
        return { micStates: p.micStates, cycles: p.cycles, order: p.liveKitOrder,
          responseSources: p.responseSourceUtterances, playback: p.playbackCompletions }
      })), contentType: 'application/json',
    })
  }
})

async function send(page: Page, message: string) {
  const input = page.getByRole('textbox', { name: 'メッセージ', exact: true })
  await expect(input).toBeEnabled()
  await input.fill(message)
  const response = page.waitForResponse(r => r.url().endsWith('/api/chat') && r.request().method() === 'POST', { timeout: 300_000 })
  await page.getByRole('button', { name: '送信', exact: true }).click()
  expect((await response).ok()).toBe(true)
  await expect(input).toBeEnabled()
}

async function waitForApprovalOrQuestion(page: Page) {
  await expect(page.getByRole('region', { name: '外部参照', exact: true }))
    .toContainText(/ハイリスク操作群・対話中|追加情報をお待ちしています/, { timeout: 10_000 })
  return await confirmation(page).count() > 0
}

async function requestTextWrite(page: Page, value: string) {
  await send(page, `検証用ファイル${sample}の内容を正確に「${value}」だけに置き換えてください。write_fileのcontentは「${value}」です。「展示テーマは」「です。」などの前後の文は含めないでください。`)
  for (let answered = 0; answered < 2 && !(await waitForApprovalOrQuestion(page)); answered++) {
    // 実LLMが対象確認や先行読取を質問した場合だけ、利用者の追加回答として具体化する。
    await expect(page.locator('article.message').last()).toContainText(/ファイル|パス|読み取|内容/)
    await send(page, `はい。対象は${sample}です。必要ならこのファイルを読み取り、内容全体を「${value}」だけに置き換えてください。`)
  }
  await expect(confirmation(page)).toBeVisible()
}

async function answerTextQuestionsWithoutApproval(page: Page, value: string) {
  const conversation = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:miori'))
  expect(conversation).toBeTruthy()
  for (let answered = 0; answered < 2; answered++) {
    // 実Backendの状態を参照して、承認要求と引数の追加質問を区別する。
    const response = await page.request.get(`/api/tool-use/status/miori/${encodeURIComponent(conversation!)}`)
    expect(response.ok()).toBe(true)
    const status = await response.json()
    expect(status.confirmation_id ?? null).toBeNull()
    if (status.state !== 'waiting') return
    await expect(confirmation(page)).toHaveCount(0)
    await expect(page.getByRole('region', { name: '外部参照', exact: true }))
      .toContainText('追加情報をお待ちしています')
    await expect(page.locator('article.message').last()).toContainText(/ファイル|パス|読み取|内容/)
    await send(page, `対象は${sample}です。ファイル全体の内容を「${value}」だけに置き換えてください。`)
  }
}

async function requestVoiceWrite(page: Page, clip: string) {
  const before = await playedResponses(page)
  await speak(page, clip)
  await expect.poll(() => playedResponses(page)).toBeGreaterThan(before)
  for (let answered = 0; answered < 2; answered++) {
    await waitForPlayback(page)
    if (await waitForApprovalOrQuestion(page)) return
    await expect(page.locator('article.message').last()).toContainText(/ファイル|パス|読み取|内容/)
    const before = await playedResponses(page)
    await speak(page, clip)
    await expect.poll(() => playedResponses(page)).toBeGreaterThan(before)
  }
  await expect(confirmation(page)).toBeVisible()
}

async function choose(page: Page, label: string) {
  await expect(confirmation(page)).toContainText('ハイリスク操作群・対話中')
  for (const option of ['常に承認する', '一度承認する', '拒否する']) {
    await expect(confirmation(page).getByRole('button', { name: option, exact: true })).toBeVisible()
  }
  const result = page.waitForResponse(r => /\/addon-actions\/requests\/[^/]+\/continue$/.test(r.url()) && r.request().method() === 'POST', { timeout: 300_000 })
  await confirmation(page).getByRole('button', { name: label, exact: true }).click()
  const response = await result
  expect(response.ok()).toBe(true)
  return new URL(response.url()).pathname.split('/').at(-2)!
}

async function playedResponses(page: Page) {
  return page.evaluate(() => new Set(window.__voiceChatE2E.liveKitOrder
    .filter(item => item.endsWith(':rendered-audio')).map(item => item.split(':')[0])).size)
}

async function expectWriteReply(page: Page, value: string) {
  const reply = page.locator('article.message').last()
  await expect(reply).toContainText(value, { timeout: 10_000 })
  await expect(reply).toContainText(
    /完了|(?:更新|変更|保存|上書き)(?:しました|されました|済み)|(?:置き換え|書き換え)(?:ました|られました)|(?:置き換わ|書き換わ)りました|書き込(?:みました|まれました)/,
    { timeout: 10_000 },
  )
}

async function waitForPlayback(page: Page, utteranceId?: string) {
  await expect.poll(() => page.evaluate((id) => {
    const probe = window.__voiceChatE2E
    const response = id
      ? Object.entries(probe.responseSourceUtterances ?? {}).find(([, sources]) => sources.includes(id))?.[0]
      : probe.cycles.at(-1)?.responseId
    if (!response) return false
    const completion = probe.playbackCompletions?.[response]
    return probe.liveKitOrder.includes(`${response}:completed`)
      && !!completion && completion.renderedSamples > 0 && completion.packetCount > 0
  }, utteranceId)).toBe(true)
}

async function installSpeech(page: Page) {
  const names = ['action-once', 'spoken-approval', 'action-reject', 'action-always']
  const clips = Object.fromEntries(await Promise.all(names.map(async name => [name,
    (await readFile(join(process.env.TOOL_USE_TEST_AUDIO_DIR!, `${name}.wav`))).toString('base64')])))
  await page.addInitScript((audio) => {
    const native = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
    let context: AudioContext
    let destination: MediaStreamAudioDestinationNode
    const play = async (name: string) => {
      const bytes = Uint8Array.from(atob(audio[name]), c => c.charCodeAt(0))
      const source = context.createBufferSource()
      source.buffer = await context.decodeAudioData(bytes.buffer)
      source.connect(destination)
      source.start()
    }
    navigator.mediaDevices.getUserMedia = async constraints => {
      const granted = await native(constraints)
      granted.getTracks().forEach(track => track.stop())
      context = new AudioContext()
      destination = context.createMediaStreamDestination()
      destination.channelCount = 1
      await context.resume()
      return destination.stream
    }
    ;(window as unknown as { __actionSpeech: typeof play }).__actionSpeech = play
  }, clips)
}

const speak = (page: Page, name: string) => page.evaluate(
  clip => (window as unknown as { __actionSpeech: (name: string) => Promise<void> }).__actionSpeech(clip), name,
)

const adminPanel = (page: Page) => page.getByRole('region', { name: '確認キュー・承認設定', exact: true })

async function openAdmin(page: Page) {
  const opener = page.getByRole('button', { name: /Addon \/ 連携/ })
  if (!(await opener.isVisible())) await page.getByRole('button', { name: 'サイドバーを開く' }).click()
  await opener.click()
  await expect(adminPanel(page)).toBeVisible()
  await expect(adminPanel(page).getByRole('button', { name: '承認状態を再取得' })).toBeEnabled()
}

async function resetConversationApproval(page: Page) {
  await openAdmin(page)
  const row = adminPanel(page).getByRole('listitem', { name: /ハイリスク操作群・対話中$/ })
  await row.getByRole('button', { name: '未承認へ戻す' }).click()
  await expect(adminPanel(page)).toContainText('未承認へ戻しました。')
  await expect(adminPanel(page).getByRole('button', { name: '承認状態を再取得' })).toBeEnabled()
  await page.getByRole('button', { name: 'チャットへ戻る', exact: true }).click()
}

async function chooseInAdmin(page: Page, label: string, live: boolean) {
  await openAdmin(page)
  const queue = adminPanel(page).getByRole('list', { name: '確認要求一覧' })
  await expect(queue.getByRole('listitem')).toHaveCount(1)
  await expect(queue).toContainText(live ? '元の操作は待機中です' : '元の操作の待機は終了しています')
  const result = page.waitForResponse(r => r.url().endsWith(live ? '/continue' : '/answer') && r.request().method() === 'POST', { timeout: 300_000 })
  await queue.getByRole('button', { name: label, exact: true }).click()
  const response = await result
  expect(response.ok()).toBe(true)
  const id = new URL(response.url()).pathname.split('/').at(-2)!
  await expect(adminPanel(page).getByRole('button', { name: '承認状態を再取得' })).toBeEnabled({ timeout: 300_000 })
  return id
}

test('管理画面から待機中のテキスト・LiveKit承認と停止後の将来単回許可を実行する', async ({ page }, info) => {
  await installSpeech(page)
  const microphone = await driver.openVoiceChat(page)
  await resetConversationApproval(page)
  const original = await sampleText()
  await send(page, `検証用ファイルは${sample}です。このパスのファイルを読んで内容を教えてください。今後も検証用ファイルとはこのパスを指します。`)
  await requestTextWrite(page, '赤い風船')
  expect(await sampleText()).toBe(original)
  await chooseInAdmin(page, '一度承認する', true)
  await expect.poll(sampleText).toBe('赤い風船')
  await page.getByRole('button', { name: 'チャットへ戻る', exact: true }).click()
  await expectWriteReply(page, '赤い風船')

  await requestTextWrite(page, '白い雲')
  // 明示停止は期限切れと同じ「待機終了」。管理画面への切替とは別に検証する。
  await page.getByRole('button', { name: '外部操作を停止', exact: true }).click()
  await expect(confirmation(page)).toHaveCount(0)
  await chooseInAdmin(page, '一度承認する', false)
  expect(await sampleText()).toBe('赤い風船')
  const setting = adminPanel(page).getByRole('listitem', { name: /ハイリスク操作群・対話中$/ })
  await expect(setting).toContainText('将来の呼び出し用の単回許可：1回')
  await page.getByRole('button', { name: 'チャットへ戻る', exact: true }).click()
  await send(page, `検証用ファイル${sample}の内容を正確に「紫の花」だけに置き換えてください。`)
  await answerTextQuestionsWithoutApproval(page, '紫の花')
  await expect.poll(sampleText).toBe('紫の花')
  await expectWriteReply(page, '紫の花')

  await microphone.click()
  await expect(microphone).toHaveClass(/mic-standby/)
  await requestVoiceWrite(page, 'action-once')
  await waitForPlayback(page)
  const voiceId = await chooseInAdmin(page, '一度承認する', true)
  await expect.poll(sampleText).toBe('青い星')
  await waitForPlayback(page, voiceId)
  await page.getByRole('button', { name: 'チャットへ戻る', exact: true }).click()
  await expectWriteReply(page, '青い星')
  await driver.endVoiceSession(page)

  await openAdmin(page)
  const autonomous = adminPanel(page).getByRole('listitem', { name: /ハイリスク操作群・会話外$/ })
  await autonomous.getByRole('button', { name: '拒否へ変更' }).click()
  await expect(autonomous).toContainText('設定：拒否')
  await autonomous.getByRole('button', { name: '未承認へ戻す' }).click()
  await expect(autonomous).toContainText('設定：未承認')
  await expect(adminPanel(page).getByRole('listitem', { name: /通常操作群・会話外$/ })).toContainText('設定：常に承認')
  await page.screenshot({ path: info.outputPath('approval-admin.png'), fullPage: true })
  await info.attach('admin-action-evidence', { body: JSON.stringify({
    textAdminContinuation: true, lateApprovalDidNotReplay: true, futureOnceConsumed: true,
    voiceAdminContinuationAndPlayback: true, autonomousSettingsIsolated: true,
  }), contentType: 'application/json' })
})

test('独立MCPへのテキスト・LiveKit会話で承認と実際の副作用を確認する', async ({ page }, info) => {
  await installSpeech(page)
  const microphone = await driver.openVoiceChat(page)

  await send(page, `検証用ファイルは${sample}です。このパスのファイルを読んで、展示テーマを教えてください。今後も検証用ファイルとはこのパスを指します。`)
  await expect(page.locator('article.message').last()).toContainText('青い折り紙')
  await expect(confirmation(page)).toHaveCount(0)

  await requestTextWrite(page, '赤い風船')
  expect(await sampleText()).toContain('青い折り紙')
  await choose(page, '一度承認する')
  await expect.poll(sampleText).toBe('赤い風船')
  await expectWriteReply(page, '赤い風船')
  await expect(confirmation(page)).toHaveCount(0)

  await requestTextWrite(page, '白い雲')
  await choose(page, '拒否する')
  await expect(confirmation(page)).toHaveCount(0)
  expect(await sampleText()).toBe('赤い風船')

  await microphone.click()
  await expect(microphone).toHaveClass(/mic-standby/)
  await requestVoiceWrite(page, 'action-once')
  await expect(confirmation(page)).toBeVisible()
  await waitForPlayback(page)
  const beforeSpokenApproval = await playedResponses(page)
  await speak(page, 'spoken-approval')
  await expect.poll(() => playedResponses(page)).toBeGreaterThan(beforeSpokenApproval)
  await waitForPlayback(page)
  expect(await sampleText()).toBe('赤い風船')
  await expect(confirmation(page).getByRole('button', { name: '一度承認する', exact: true })).toBeVisible()
  const onceId = await choose(page, '一度承認する')
  await expect.poll(sampleText).toBe('青い星')
  await waitForPlayback(page, onceId)
  await expectWriteReply(page, '青い星')
  await expect(confirmation(page)).toHaveCount(0)

  await requestVoiceWrite(page, 'action-reject')
  await expect(confirmation(page)).toBeVisible()
  await waitForPlayback(page)
  const rejectId = await choose(page, '拒否する')
  await expect(confirmation(page)).toHaveCount(0)
  await waitForPlayback(page, rejectId)
  expect(await sampleText()).toBe('青い星')

  await requestVoiceWrite(page, 'action-always')
  await expect(confirmation(page)).toBeVisible()
  await waitForPlayback(page)
  const alwaysId = await choose(page, '常に承認する')
  await expect.poll(sampleText).toBe('金の花')
  await waitForPlayback(page, alwaysId)
  await expectWriteReply(page, '金の花')
  await driver.endVoiceSession(page)

  await send(page, `検証用ファイル${sample}の内容を正確に「紫の花」だけに置き換えてください。`)
  await answerTextQuestionsWithoutApproval(page, '紫の花')
  await expect.poll(sampleText).toBe('紫の花')
  await expectWriteReply(page, '紫の花')
  await expect(confirmation(page)).toHaveCount(0)
  await info.attach('action-evidence', {
    body: JSON.stringify({
      normalReadWithoutConfirmation: true, textOnce: true, textReject: true,
      spokenApprovalIgnored: true, voiceOnce: true, voiceReject: true,
      voiceAlways: true, subsequentWriteWithoutConfirmation: true,
      renderedResponses: await playedResponses(page), finalExternalState: await sampleText(),
      // 合成試験の回答を保存し、実行予告だけで結果回答済みとしない。
      syntheticReplies: await page.locator('article.message').allTextContents(),
      playback: await page.evaluate(() => Object.entries(window.__voiceChatE2E.playbackCompletions ?? {}).map(([responseId, c]) => ({
        responseId, sourceUtteranceIds: window.__voiceChatE2E.responseSourceUtterances?.[responseId] ?? [],
        expectedSamples: c.expectedSamples, renderedSamples: c.renderedSamples, packetCount: c.packetCount,
      }))),
    }), contentType: 'application/json',
  })
})
