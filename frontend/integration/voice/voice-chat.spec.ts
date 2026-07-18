import { expect, test } from '@playwright/test'

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
test.setTimeout(voiceTestTimeout)

const driver = createVoiceChatDriver()

test('マイクボタン操作でOFFからSTANDBYへ遷移する', async ({ page }) => {
  const button = await driver.openVoiceChat(page)
  await expect(button).not.toHaveClass(/mic-standby|mic-active/)
  await button.click()
  await expect(button).toHaveClass(/mic-standby/)
})

test('VADの発話イベント中だけON表示になり、発話終了後にSTANDBYへ戻る', async ({ page }) => {
  const button = await driver.enableMicrophone(page)
  await expect(button).toHaveClass(/mic-active/, { timeout: 15_000 })
  await driver.expectMicrophoneStandby(page)
})

test('実音声応答のuser発話とmiori応答がこの順でチャット欄に表示される', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.expectMessages(page)
})

test('実音声応答はuser text、miori text、audio binaryの順で受信する', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.expectMessages(page)
  await expect(driver.waitForFrameOrder(page)).resolves.toEqual(['user-text', 'miori-text', 'audio'])
})

test('実音声応答のバイナリフレームを受信するとブラウザで音声再生を開始する', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.waitForCompletedVoiceCycle(page)
})

test('実音声送信から音声再生開始までの遅延を証跡へ添付する', async ({ page }, testInfo) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  const cycle = await driver.waitForCompletedVoiceCycle(page)
  await testInfo.attach('voice-playback-latency.json', {
    body: JSON.stringify(cycle, null, 2),
    contentType: 'application/json',
  })
})
