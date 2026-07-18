import { expect, test } from '@playwright/test'

import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../../playwright/resolved-profile'

let resolvedProfile: ResolvedProfile
const REAL_RESPONSE_TIMEOUT_MS = 60_000

test.describe.configure({ mode: 'serial' })
test.setTimeout(REAL_RESPONSE_TIMEOUT_MS + 30_000)

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({}, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'text-chat-real')
  if (reason !== null) {
    test.skip(true, reason)
  }
})

test('実サービスから受け取った光織の応答がチャット画面に表示される', async ({ page }) => {
  await page.goto('/')

  const input = page.getByLabel('メッセージ')
  await input.fill('こんにちは')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('こんにちは', { exact: true })).toBeVisible()
  const mioriMessage = page.locator('article.message').nth(1)
  await expect(mioriMessage).toBeVisible({ timeout: REAL_RESPONSE_TIMEOUT_MS })
  await expect(mioriMessage.locator('.speaker')).toHaveText('光織')
  await expect(mioriMessage.locator('p')).not.toHaveText('')
  await expect(mioriMessage.locator('p')).not.toHaveText('応答の取得に失敗しました。')
  await expect(input).toHaveValue('')
})
