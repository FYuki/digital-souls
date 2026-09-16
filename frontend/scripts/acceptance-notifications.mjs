// 実HTTPと独立MCP processを使う。通信の差し替えを行わない。
import { chromium, expect } from '@playwright/test'
import { readFileSync, writeFileSync, renameSync } from 'node:fs'
import { join } from 'node:path'
const [url, root, output] = process.argv.slice(2)
const checks = []
const browser = await chromium.launch({ headless: true })
const statePath = join(root, 'provider/state.json')
function provider(values) {
  const state = JSON.parse(readFileSync(statePath, 'utf8'))
  const temp = statePath + '.next'
  writeFileSync(temp, JSON.stringify({ ...state, ...values }))
  renameSync(temp, statePath)
}
async function received(position) {
  await expect.poll(() => {
    try { return JSON.parse(readFileSync(join(root, 'checkpoint.json'), 'utf8')).position } catch { return 0 }
  }, { timeout: 30000 }).toBe(position)
}
async function open() {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  const ready = await page.request.get(url + '/api/health/ready')
  expect(await ready.json()).toEqual({ status: 'ready', mode: 'notifications_only' })
  expect((await page.request.get(url + '/api/health/inference')).status()).toBe(503)
  await page.goto(url)
  await page.getByRole('button', { name: /通知/ }).first().click()
  await page.getByRole('heading', { name: /通知/ }).first().waitFor()
  await expect(page.getByText(/会話機能は現在利用できません/)).toBeVisible()
  return page
}
try {
  await received(10)
  let page = await open()
  provider({ position: 12, detail_text: '合成した更新情報です。 https://private.invalid/mcp' })
  await received(12)
  await expect.poll(async () => (await (await page.request.get(url + '/api/notifications')).json()).total, { timeout: 20000 }).toBe(2)
  await page.getByRole('button', { name: '再取得', exact: true }).click()
  await expect(page.getByRole('list', { name: '通知一覧' }).getByRole('listitem')).toHaveCount(2)
  await expect(page.getByText('2件未読', { exact: true })).toBeVisible()
  checks.push('individual-notifications-without-conversation-or-llm')
  await page.close()
  provider({ position: 13 })
  await received(13)
  const context = await browser.newContext()
  await expect.poll(async () => (await (await context.request.get(url + '/api/notifications')).json()).retention.history_incomplete,
                    { timeout: 20000 }).toBe(true)
  await context.close()
  page = await open()
  await expect(page.getByText(/件数上限により削除された通知があります/)).toBeVisible()
  await expect(page.getByText('2件未読', { exact: true })).toBeVisible()
  checks.push('background-save-and-cap-with-browser-closed')
  await page.getByRole('button', { name: '処理完了 task-13の詳細' }).click()
  await expect(page.getByText('合成した更新情報です。 [外部参照]', { exact: true })).toBeVisible()
  await expect(page.getByText('2件未読', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: '確認して既読にする' }).click()
  await expect(page.getByText('1件未読', { exact: true })).toBeVisible()
  checks.push('sanitized-detail-and-explicit-read')
  await page.getByRole('button', { name: '詳細を閉じる' }).click()
  await page.getByRole('button', { name: '通知設定', exact: true }).click()
  const toggle = page.getByRole('switch', { name: 'fixture 処理完了の通知' })
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  provider({ position: 14 })
  await received(14)
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'true')
  provider({ position: 15 })
  await received(15)
  await expect.poll(async () => {
    const data = await (await page.request.get(url + '/api/notifications')).json()
    return data.items.map(item => item.metadata.task_ref)
  }, { timeout: 20000 }).toContain('task-15')
  const data = await (await page.request.get(url + '/api/notifications')).json()
  expect(data.items.map(item => item.metadata.task_ref)).not.toContain('task-14')
  checks.push('off-does-not-stop-ingestion-and-on-does-not-backfill')
  await page.getByRole('button', { name: '再取得', exact: true }).click()
  await page.getByLabel('通知元', { exact: true }).selectOption('fixture')
  await page.getByLabel('担当', { exact: true }).selectOption('miori')
  await page.getByLabel('未読のみ', { exact: true }).check()
  await expect(page.getByRole('list', { name: '通知一覧' }).getByRole('listitem')).toHaveCount(1)
  checks.push('source-character-unread-filters')
  await page.screenshot({ path: join(output, 'desktop.png') })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: join(output, 'mobile.png') })
  const retained = data.items[0]
  provider({ detail_state: 'expired' })
  await page.getByRole('button', { name: '処理完了 ' + retained.metadata.task_ref + 'の詳細' }).click()
  await expect(page.getByText('提供元で保存期限が切れています。', { exact: true })).toBeVisible()
  checks.push('expired-result-is-not-success')
  console.log(JSON.stringify({ passed: true, checks }))
} finally { await browser.close() }
