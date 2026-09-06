import { expect, test, type Page } from '@playwright/test'
import { access, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { createVoiceChatDriver } from '../../playwright/voice-chat-suite'

// 外部MCPは制御fixture。STT/LLM/TTS/LiveKitとbrowserは実接続する。
const driver = createVoiceChatDriver()
const signals = process.env.TOOL_USE_TEST_SIGNALS!

test.afterEach(async ({ page }, info) => {
  if (info.status !== info.expectedStatus) {
    await info.attach('synthetic-conversation', { body: JSON.stringify(await page.locator('article.message').allTextContents()), contentType: 'application/json' })
  }
})

async function send(page: Page, message: string) {
  const input = page.getByRole('textbox', { name: 'メッセージ', exact: true })
  await expect(input).toBeEnabled()
  await input.fill(message)
  const response = page.waitForResponse(r => r.url().endsWith('/api/chat') && r.request().method() === 'POST', { timeout: 180_000 })
  await page.getByRole('button', { name: '送信', exact: true }).click()
  return await response
}

async function signal(name: string) {
  try { await access(join(signals, name)); return true } catch { return false }
}

test('テキストで追加情報を質問し、回答で同じ操作を再開する', async ({ page }) => {
  await driver.openVoiceChat(page)
  expect((await send(page, '展示の案内を準備してください。')).ok()).toBe(true)
  await expect(page.getByRole('region', { name: '外部参照' })).toContainText('追加情報をお待ちしています')
  await expect(page.locator('article.message').last()).toContainText('色', { timeout: 5_000 })
  expect((await send(page, '赤色です。')).ok()).toBe(true)
  await expect(page.locator('article.message').last()).toContainText('赤', { timeout: 5_000 })
  expect(await signal('resumed')).toBe(true)
  await expect(page.getByRole('region', { name: '外部参照' })).toContainText('今回参照した情報')
})

test('テキストの実行中に停止すると遅い結果が回答へ混ざらない', async ({ page }) => {
  await driver.openVoiceChat(page)
  const reply = send(page, 'slow-exhibitで展示の遅い照合を実行してください。')
  await expect.poll(() => signal('slow-dispatched')).toBe(true)
  await page.getByRole('button', { name: '外部操作を停止', exact: true }).click()
  expect((await reply).status()).toBe(499)
  await expect(page.getByRole('textbox', { name: 'メッセージ', exact: true })).toBeEnabled()
  await expect(page.locator('article.message')).not.toContainText(['遅い照合の結果は金色'])
})

test('音声で追加質問を再生し、音声の回答で再開して最終回答を再生する', async ({ page }) => {
  await rm(join(signals, 'resumed'), { force: true })
  const microphone = await driver.openVoiceChat(page)
  await microphone.click()
  await expect(page.getByRole('region', { name: '外部参照' })).toContainText('追加情報をお待ちしています')
  await expect.poll(() => page.evaluate(() => window.__voiceChatE2E.liveKitOrder.filter(item => item.endsWith(':rendered-audio')).length)).toBeGreaterThan(0)
  await expect.poll(() => signal('resumed'), { timeout: 150_000 }).toBe(true)
  await expect(page.locator('article.message').last()).toContainText('赤')
  await expect.poll(() => page.evaluate(() => new Set(window.__voiceChatE2E.liveKitOrder.filter(item => item.endsWith(':rendered-audio')).map(item => item.split(':')[0])).size)).toBeGreaterThan(1)
  await driver.endVoiceSession(page)
})
