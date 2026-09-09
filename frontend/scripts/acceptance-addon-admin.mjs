// 実HTTP APIと公開MCPを使う。page.routeや通信の置換は行わない。
import { chromium, expect } from '@playwright/test'
import { join } from 'node:path'
const [url, phase, output] = process.argv.slice(2)
const passed = (check) => console.log('ADDON_ACCEPTANCE ' + JSON.stringify({ check, status: 'passed' }))
const browser = await chromium.launch({ headless: true })
try {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
  await page.goto(url)
  await page.getByRole('button', { name: /Addon \/ 連携/ }).click()
  const row = page.getByRole('listitem').filter({ has: page.getByRole('heading', { name: '受入用の公開MCP', exact: true }) })
  const toggle = row.getByRole('switch', { name: '受入用の公開MCPを利用する' })
  await toggle.waitFor()
  if (phase === 'initial') {
    const list = page.getByRole('list', { name: '登録済みの連携' })
    await expect(list.getByRole('listitem')).toHaveCount(3)
    for (const name of ['受入用の公開MCP', '受入用の常時ON接続', '受入用の未接続OFF']) {
      await expect(list.getByRole('heading', { name, exact: true })).toBeVisible()
    }
    const offRow = list.getByRole('listitem').filter({ has: page.getByRole('heading', { name: '受入用の未接続OFF', exact: true }) })
    await expect(offRow.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
    await expect(offRow.getByText('無効', { exact: true })).toBeVisible()
    const registered = await page.request.get(new URL('/api/addon-admin/connections', url).href)
    expect(registered.ok()).toBe(true)
    const statuses = await registered.json()
    expect(statuses.find((item) => item.connection_instance_id === 'acceptance-disabled')).toMatchObject({
      availability: 'unknown', desired_enabled: false, last_checked_at: null,
    })
    passed('registered-list')
    await row.getByText('使用可能', { exact: true }).waitFor({ timeout: 30000 })
    passed('initial-available')
    await toggle.click()
    await row.getByText('無効', { exact: true }).waitFor()
    passed('toggle-off')
  } else if (phase === 'restored') {
    if (await toggle.getAttribute('aria-checked') !== 'false') throw new Error('希望OFFが復元されていません')
    passed('restart-restores-off')
    await toggle.click()
    await row.getByText('使用可能', { exact: true }).waitFor({ timeout: 30000 })
    passed('on-rechecks-health')
  } else if (phase === 'disconnected') {
    await row.getByText(/^使用不可/).waitFor({ timeout: 110000 })
    await page.getByLabel('連携に接続エラーがあります').waitFor()
    passed('idle-disconnect-badge')
    await toggle.click()
    await row.getByText('無効', { exact: true }).waitFor()
    passed('offline-toggle')
  } else throw new Error('不明な受入段階')
  await page.screenshot({ path: join(output, `${phase}.png`) })
} finally { await browser.close() }
