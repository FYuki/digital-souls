import { expect, test } from '@playwright/test'
import { installMockUiBootstrap } from './mock-ui-bootstrap'
import { attachProfileEvidence, getCapabilitySkipReason, readResolvedProfile, type ResolvedProfile } from '../playwright/resolved-profile'
import type { AddonStatus } from '../src/lib/addon-admin/client'

let resolvedProfile: ResolvedProfile
test.beforeAll(async () => { resolvedProfile = await readResolvedProfile() })
test.beforeEach(async ({}, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'mocked-e2e')
  if (reason !== null) test.skip(true, reason)
})

for (const width of [1280, 320]) {
  test(`連携の一覧・障害badge・ON/OFF・focus復帰 ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 })
    await page.route('**/api/**', async (route) => { await route.fulfill({ json: [] }) })
    await installMockUiBootstrap(page)
    let items: AddonStatus[] = [
      { connection_instance_id: 'off', display_name: '無効な連携', source_type: 'external', desired_enabled: false, availability: 'unavailable', effective_state: 'disabled', error_code: 'connection_failed', last_checked_at: null },
      { connection_instance_id: 'available', display_name: '資料検索', source_type: 'external', desired_enabled: true, availability: 'available', effective_state: 'available', error_code: null, last_checked_at: null },
      { connection_instance_id: 'error', display_name: '認証切れの連携', source_type: 'external', desired_enabled: true, availability: 'unavailable', effective_state: 'unavailable', error_code: 'authentication_failed', last_checked_at: null },
      { connection_instance_id: 'self', display_name: '自作の記録', source_type: 'self_owned', desired_enabled: true, availability: 'degraded', effective_state: 'degraded', error_code: 'partial_failure', last_checked_at: null },
    ]
    await page.route('**/api/addon-admin/connections**', async (route) => {
      if (route.request().method() === 'PATCH') {
        const id = new URL(route.request().url()).pathname.split('/').at(-1)
        const body = route.request().postDataJSON() as { desired_enabled: boolean }
        expect(Object.keys(body)).toEqual(['desired_enabled'])
        items = items.map((item) => item.connection_instance_id === id ? { ...item, desired_enabled: body.desired_enabled, effective_state: body.desired_enabled ? item.availability : 'disabled' } : item)
        await route.fulfill({ json: items.find((item) => item.connection_instance_id === id) })
      } else await route.fulfill({ json: items })
    })
    await page.goto('/')
    if (width === 320) await page.getByRole('button', { name: 'サイドバーを開く' }).click()
    await expect(page.getByLabel('連携に接続エラーがあります')).toBeVisible()
    const opener = page.getByRole('button', { name: /Addon \/ 連携/ })
    await opener.click()
    await expect(page.getByRole('heading', { name: 'Addon / 連携' })).toBeFocused()
    await expect(page.getByRole('list', { name: '登録済みの連携' }).getByRole('heading')).toHaveText(['自作の記録', '認証切れの連携', '資料検索', '無効な連携'])
    const toggle = page.getByRole('switch', { name: '認証切れの連携を利用する' })
    await toggle.focus()
    await page.keyboard.press('Space')
    await expect(toggle).toHaveAttribute('aria-checked', 'false')
    await expect(toggle).toBeFocused()
    await expect(page.getByRole('list', { name: '登録済みの連携' })).toContainText('無効')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    const screenshot = testInfo.outputPath(`addon-admin-${width}.png`)
    await page.screenshot({ path: screenshot })
    await testInfo.attach(`addon-admin-${width}`, { path: screenshot, contentType: 'image/png' })
    await page.getByRole('button', { name: 'チャットへ戻る' }).click()
    await expect(opener).toBeFocused()
    await expect(page.getByLabel('連携の一部機能に問題があります')).toBeVisible()
    await expect(page.getByLabel('連携に接続エラーがあります')).toHaveCount(0)
  })
}
