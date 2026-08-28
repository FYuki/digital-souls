import { expect, test } from '@playwright/test'

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

test.beforeEach(async ({}, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'voice-chat-real')
  if (reason !== null) test.skip(true, reason)
})

test.afterEach(async ({ page }) => {
  await driver.endVoiceSession(page)
  await hardDeleteSelectedConversation(page, 'miori')
})

test.use(createVoiceTestUseOptions())
test.describe.configure({ mode: 'serial' })
test.setTimeout(voiceTestTimeout)

const driver = createVoiceChatDriver()

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
  const button = await driver.enableMicrophone(page)
  await expect(button).toHaveClass(/mic-active/, { timeout: 15_000 })
  await driver.expectMicrophoneStandby(page)
  await expect(button).toHaveAttribute('aria-pressed', 'true')
})

test('実音声応答のuser発話とmiori応答がこの順でチャット欄に表示される', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.expectMessages(page)
  await expect(driver.waitForLiveKitStreamingOrder(page)).resolves.toEqual([
    'text-delta',
    'first-audio-out',
    'completed',
  ])
})
