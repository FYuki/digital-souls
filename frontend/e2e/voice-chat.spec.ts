import { expect, test, type Page } from '@playwright/test'

import {
  createVoiceChatDriver,
  createVoiceTestUseOptions,
  voiceTestTimeout,
} from '../playwright/voice-chat-suite'
import { installMockWebSocketBackend } from './mock-web-socket'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../playwright/resolved-profile'

const MOCK_TRANSCRIPT_TEXT = 'テスト音声です'
const MOCK_RESPONSE_TEXT = 'テスト音声に応答します。'
let resolvedProfile: ResolvedProfile

const createMockAudioResponse = (): Buffer => {
  const sampleRate = 16_000
  const samples = Math.floor(sampleRate * 0.2)
  const dataSize = samples * 2
  const buffer = Buffer.alloc(44 + dataSize)
  buffer.write('RIFF', 0)
  buffer.writeUInt32LE(36 + dataSize, 4)
  buffer.write('WAVE', 8)
  buffer.write('fmt ', 12)
  buffer.writeUInt32LE(16, 16)
  buffer.writeUInt16LE(1, 20)
  buffer.writeUInt16LE(1, 22)
  buffer.writeUInt32LE(sampleRate, 24)
  buffer.writeUInt32LE(sampleRate * 2, 28)
  buffer.writeUInt16LE(2, 32)
  buffer.writeUInt16LE(16, 34)
  buffer.write('data', 36)
  buffer.writeUInt32LE(dataSize, 40)
  for (let index = 0; index < samples; index += 1) {
    buffer.writeInt16LE(
      Math.round(Math.sin((2 * Math.PI * 440 * index) / sampleRate) * 12_000),
      44 + index * 2,
    )
  }
  return buffer
}

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
  await installMockWebSocketBackend(page, {
    textFrames: [],
    binaryFrames: [
      { kind: 'text', data: JSON.stringify({ type: 'text', speaker: 'user', message: MOCK_TRANSCRIPT_TEXT }) },
      { kind: 'text', data: JSON.stringify({ type: 'text', speaker: 'miori', response: MOCK_RESPONSE_TEXT }) },
      { kind: 'binary', data: Array.from(createMockAudioResponse()) },
    ],
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

test('音声応答のuser発話とmiori応答がこの順でチャット欄に表示される', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.expectMessages(page)
  await expectMockMessages(page)
})

test('音声応答はuser text、miori text、audio binaryの順で受信する', async ({ page }) => {
  await driver.enableMicrophone(page)
  await driver.waitForSpeechCompletion(page)
  await driver.expectMessages(page)
  await expect(driver.waitForFrameOrder(page)).resolves.toEqual(['user-text', 'miori-text', 'audio'])
})

test('音声応答のバイナリフレームを受信するとブラウザで音声再生を開始する', async ({ page }) => {
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
