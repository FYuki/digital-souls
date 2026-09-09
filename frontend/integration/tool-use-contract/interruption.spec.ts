import { expect, test } from '@playwright/test'
import { access, readFile, rm } from 'node:fs/promises'
import { join } from 'node:path'
import { createVoiceChatDriver } from '../../playwright/voice-chat-suite'
import { playClip, prepareMicrophone } from './voice-fixture'

const driver = createVoiceChatDriver()
const signals = process.env.TOOL_USE_TEST_SIGNALS!
test.afterEach(async ({ page }, info) => {
  if (info.status !== info.expectedStatus) {
    await info.attach('synthetic-conversation', { body: JSON.stringify(await page.locator('article.message').allTextContents()), contentType: 'application/json' })
  }
})
async function signal(name: string) {
  try { await access(join(signals, name)); return true } catch { return false }
}

for (const kind of ['停止', '割り込み'] as const) {
  test(`音声の外部処理実行中に${kind}すると旧結果を読み上げず再実行しない`, async ({ page }) => {
    for (const name of ['slow-dispatched', 'slow-result-attempted', 'calls.txt']) await rm(join(signals, name), { force: true })
    await prepareMicrophone(page, 'slow')
    const microphone = await driver.openVoiceChat(page)
    // 対象名を会話で確定し、音声での実行開始以降の中断を測定する。
    const input = page.getByRole('textbox', { name: 'メッセージ', exact: true })
    await input.fill('次に音声で時間のかかる確認を頼んだら、引数なしのslow-exhibitを使ってください。確認の内容は設定済みです。今は実行せず、次の依頼を待ってください。')
    const primed = page.waitForResponse(r => r.url().endsWith('/api/chat') && r.request().method() === 'POST', { timeout: 180_000 })
    await page.getByRole('button', { name: '送信', exact: true }).click()
    expect((await primed).ok()).toBe(true)
    expect(await signal('slow-dispatched')).toBe(false)
    await microphone.click()
    await expect.poll(() => signal('slow-dispatched')).toBe(true)
    const before = await page.evaluate(() => window.__voiceChatE2E.liveKitOrder.filter(item => item.endsWith(':rendered-audio')))
    if (kind === '停止') {
      await page.getByRole('button', { name: '外部操作を停止', exact: true }).click()
      await expect(page.getByRole('button', { name: '音声会話を終了', exact: true })).toBeHidden()
    } else {
      await playClip(page, 'greeting')
      await expect.poll(() => page.evaluate(() => window.__voiceChatE2E.interruptions.length)).toBeGreaterThan(0)
      await expect.poll(() => page.evaluate(() => window.__voiceChatE2E.liveKitOrder.filter(item => item.endsWith(':rendered-audio')).length)).toBeGreaterThan(before.length)
      await expect(page.locator('article.message').last()).toContainText(/こんにちは|おはよう|こんばんは/)
    }
    await expect.poll(() => signal('slow-result-attempted'), { timeout: 45_000 }).toBe(true)
    await expect(page.locator('article.message').filter({ hasText: '遅い照合の結果は金色' })).toHaveCount(0)
    expect((await readFile(join(signals, 'calls.txt'), 'utf-8')).trim().split('\n')).toEqual(['slow-exhibit'])
    if (kind === '停止') {
      expect(await page.evaluate(() => window.__voiceChatE2E.liveKitOrder.filter(item => item.endsWith(':rendered-audio')))).toEqual(before)
    } else {
      const interruptedId = await page.evaluate(() => window.__voiceChatE2E.interruptions.at(-1)!.responseId)
      expect(await page.evaluate(id => window.__voiceChatE2E.liveKitOrder.includes(`${id}:rendered-audio`), interruptedId)).toBe(false)
      await driver.endVoiceSession(page)
    }
  })
}
