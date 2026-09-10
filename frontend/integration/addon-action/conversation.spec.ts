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
        return { micStates: p.micStates, cycles: p.cycles, order: p.liveKitOrder, playback: p.playbackCompletions }
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
  await send(page, `検証用ファイル${sample}の内容を正確に「${value}」だけに置き換えてください。`)
  for (let answered = 0; answered < 2 && !(await waitForApprovalOrQuestion(page)); answered++) {
    // 実LLMが対象確認や先行読取を質問した場合だけ、利用者の追加回答として具体化する。
    await expect(page.locator('article.message').last()).toContainText(/ファイル|パス|読み取|内容/)
    await send(page, `はい。対象は${sample}です。必要ならこのファイルを読み取り、内容全体を「${value}」だけに置き換えてください。`)
  }
  await expect(confirmation(page)).toBeVisible()
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

async function waitForPlayback(page: Page, utteranceId?: string) {
  await expect.poll(() => page.evaluate((id) => {
    const probe = window.__voiceChatE2E
    const cycle = id ? probe.cycles.find(c => c.utteranceId === id) : probe.cycles.at(-1)
    const response = cycle?.responseId
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
  await driver.endVoiceSession(page)

  await send(page, `検証用ファイル${sample}の内容を正確に「紫の花」だけに置き換えてください。`)
  await expect.poll(sampleText).toBe('紫の花')
  await expect(confirmation(page)).toHaveCount(0)
  await info.attach('action-evidence', {
    body: JSON.stringify({
      normalReadWithoutConfirmation: true, textOnce: true, textReject: true,
      spokenApprovalIgnored: true, voiceOnce: true, voiceReject: true,
      voiceAlways: true, subsequentWriteWithoutConfirmation: true,
      renderedResponses: await playedResponses(page), finalExternalState: await sampleText(),
      playback: await page.evaluate(() => Object.values(window.__voiceChatE2E.playbackCompletions ?? {}).map(c => ({
        expectedSamples: c.expectedSamples, renderedSamples: c.renderedSamples, packetCount: c.packetCount,
      }))),
    }), contentType: 'application/json',
  })
})
