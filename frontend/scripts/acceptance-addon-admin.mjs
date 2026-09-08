// 実HTTP APIと公開MCPを使う。page.routeや通信の置換は行わない。
import { chromium } from '@playwright/test'
import { join } from 'node:path'
const [url, phase, output] = process.argv.slice(2)
const passed = (check) => console.log('ADDON_ACCEPTANCE ' + JSON.stringify({ check, status: 'passed' }))
const browser = await chromium.launch({ headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(url)
  await page.getByRole('button', { name: /Addon \/ 連携/ }).click()
  const toggle = page.getByRole('switch', { name: '受入用の公開MCPを利用する' })
  await toggle.waitFor()
  if (phase === 'initial') {
    passed('registered-list')
    await page.getByText('使用可能', { exact: true }).waitFor({ timeout: 30000 })
    passed('initial-available')
    await toggle.click()
    await page.getByText('無効', { exact: true }).waitFor()
    passed('toggle-off')
  } else if (phase === 'restored') {
    if (await toggle.getAttribute('aria-checked') !== 'false') throw new Error('希望OFFが復元されていません')
    passed('restart-restores-off')
    await toggle.click()
    await page.getByText('使用可能', { exact: true }).waitFor({ timeout: 30000 })
    passed('on-rechecks-health')
  } else if (phase === 'disconnected') {
    await page.getByText(/^使用不可/).waitFor({ timeout: 110000 })
    await page.getByLabel('連携に接続エラーがあります').waitFor()
    passed('idle-disconnect-badge')
    await toggle.click()
    await page.getByText('無効', { exact: true }).waitFor()
    passed('offline-toggle')
  } else throw new Error('不明な受入段階')
  await page.screenshot({ path: join(output, `${phase}.png`) })
} finally { await browser.close() }
