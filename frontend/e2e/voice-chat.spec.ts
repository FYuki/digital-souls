import { expect, test, type Page } from '@playwright/test'

import {
  createVoiceChatDriver,
  createVoiceTestUseOptions,
  voiceTestTimeout,
} from '../playwright/voice-chat-suite'
import { installMockLiveKit } from './mock-livekit'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../playwright/resolved-profile'

const MOCK_TRANSCRIPT_TEXT = 'テスト音声です'
const MOCK_RESPONSE_TEXT = 'テスト音声に応答します。'
const MOCK_CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
let resolvedProfile: ResolvedProfile

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({ page }, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'mocked-e2e')
  if (reason !== null) test.skip(true, reason)
  await installMockBackend(page)
})

const installMockBackend = async (page: Page) => {
  await installMockLiveKit(page, {
    transcript: MOCK_TRANSCRIPT_TEXT,
    response: MOCK_RESPONSE_TEXT,
  })
  await page.route('**/api/characters/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.endsWith('/turns')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
      return
    }
    const conversation = {
      character_id: 'miori',
      conversation_id: MOCK_CONVERSATION_ID,
      created_at: '2026-08-01T12:00:00.000000Z',
      updated_at: '2026-08-01T12:00:00.000000Z',
      archived_at: null,
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(request.method() === 'POST' ? conversation : []),
    })
  })
}

test.use(createVoiceTestUseOptions())
test.describe.configure({ mode: 'serial' })
test.setTimeout(voiceTestTimeout)

const driver = createVoiceChatDriver()

const expectMockMessages = async (page: Page) => {
  const messages = page.locator('article.message')
  await expect(messages.nth(0).locator('p')).toHaveText(MOCK_TRANSCRIPT_TEXT)
  await expect(messages.nth(1).locator('p')).toHaveText(MOCK_RESPONSE_TEXT)
}

test('マイクボタン操作でOFFから有効状態へ遷移する', async ({ page }) => {
  const button = await driver.openVoiceChat(page)
  await expect(button).not.toHaveClass(/mic-standby|mic-active/)
  await button.click()
  await expect(button).toHaveAttribute('aria-pressed', 'true')
  await expect(button).toHaveClass(/mic-standby|mic-active/)
})

test('VADの発話イベント中も継続microphone sessionを維持する', async ({ page }) => {
  const button = await driver.enableMicrophone(page)
  await expect(button).toHaveClass(/mic-active/, { timeout: 15_000 })
  await page.waitForFunction(() => window.__voiceChatE2E.cycles.length > 0)
  await expect(button).toHaveAttribute('aria-pressed', 'true')
})

test('音声応答のuser発話とmiori応答がこの順でチャット欄に表示される', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.expectMessages(page)
  await expectMockMessages(page)
})

test('音声応答はtext delta、audio segmentの順で受信する', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.expectMessages(page)
  await expect(driver.waitForFrameOrder(page)).resolves.toEqual(['text-delta', 'audio'])
})

test('音声segmentを受信すると再生開始観測をresponseへ相関する', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.waitForCompletedVoiceCycle(page)
})

test('音声送信から音声再生開始までの遅延を計測してレポートへ添付する', async ({ page }, testInfo) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  const cycle = await driver.waitForCompletedVoiceCycle(page)
  await testInfo.attach('voice-playback-latency.json', {
    body: JSON.stringify(cycle, null, 2),
    contentType: 'application/json',
  })
})
