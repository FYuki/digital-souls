import { test, expect, type Page } from '@playwright/test'
import { writeFile } from 'node:fs/promises'
import { createVoiceChatDriver } from '../../playwright/voice-chat-suite'

const sample = process.env.TOOL_USE_TEST_SAMPLE!
const driver = createVoiceChatDriver()

async function send(page: Page, message: string) {
  const input = page.getByRole('textbox', { name: 'メッセージ', exact: true })
  await expect(input).toBeEnabled()
  await input.fill(message)
  const result = page.waitForResponse(r => r.url().endsWith('/api/chat') && r.request().method() === 'POST', { timeout: 180_000 })
  await page.getByRole('button', { name: '送信', exact: true }).click()
  expect((await result).ok()).toBe(true)
  await expect(input).toBeEnabled()
}

test('実ブラウザのテキスト会話でstdio ToolとHTTP Resourceの結果を回答する', async ({ page }) => {
  await driver.openVoiceChat(page)
  await send(page, `${sample}を読んで、展示テーマを教えてください。`)
  await expect(page.locator('article.message').last()).toContainText('青い折り紙', { timeout: 5_000 })
  await expect(page.getByRole('region', { name: '外部参照' })).toContainText('今回参照した情報')
  await send(page, '外部資料startup.mdを読んで、Everything Serverの起動方法を短く教えてください。')
  await expect(page.locator('article.message').last()).toContainText(/stdio|streamableHttp|index\.js/, { timeout: 5_000 })
  await page.getByText('今回参照した情報').click()
  await expect(page.getByRole('region', { name: '外部参照' })).toContainText('startup.md')
})

for (const kind of ['filesystem', 'resource'] as const) {
  test.describe(`実LiveKit音声/${kind}`, () => {

    test('音声入力から外部取得・回答・AudioTrack再生まで通る', async ({ page }) => {
      const microphone = await driver.openVoiceChat(page)
      if (kind === 'filesystem') {
        await send(page, `次の質問で使うファイルは${sample}です。今は読まずに、このパスを会話の中で覚えておいてください。`)
        await writeFile(sample, '展示テーマは赤い風船です。', 'utf-8')
      }
      await microphone.click()
      await expect.poll(() => page.evaluate(() => window.__voiceChatE2E.liveKitOrder.some(item => item.endsWith(':rendered-audio'))), { timeout: 180_000 }).toBe(true)
      await expect(page.locator('article.message').last()).toContainText(kind === 'filesystem' ? '赤い風船' : /stdio|起動|サーバー/)
      await expect(page.getByRole('region', { name: '外部参照' })).toContainText('今回参照した情報')
      if (kind === 'resource') {
        await page.getByText('今回参照した情報').click()
        await expect(page.getByRole('region', { name: '外部参照' })).toContainText('startup.md')
      }
      await driver.endVoiceSession(page)
    })
  })
}
