// 実HTTP APIと公開MCPを使う。page.routeや通信の置換は行わない。
import { chromium } from '@playwright/test'
import { join } from 'node:path'
const [url, phase, output] = process.argv.slice(2)
const browser = await chromium.launch({ headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(url)
  await page.getByRole('button', { name: /Addon \/ 連携/ }).click()
  const toggle = page.getByRole('switch', { name: '受入用の公開MCPを利用する' })
  await toggle.waitFor()
  if (phase === 'initial') {
    await page.getByText('使用可能', { exact: true }).waitFor({ timeout: 30000 })
    await toggle.click()
    await page.getByText('無効', { exact: true }).waitFor()
  } else if (phase === 'restored') {
    if (await toggle.getAttribute('aria-checked') !== 'false') throw new Error('希望OFFが復元されていません')
    await toggle.click()
    await page.getByText('使用可能', { exact: true }).waitFor({ timeout: 30000 })
  } else if (phase === 'disconnected') {
    await page.getByText(/^使用不可/).waitFor({ timeout: 110000 })
    await page.getByLabel('連携に接続エラーがあります').waitFor()
    await toggle.click()
    await page.getByText('無効', { exact: true }).waitFor()
  } else throw new Error('不明な受入段階')
  await page.screenshot({ path: join(output, `${phase}.png`) })
} finally { await browser.close() }
