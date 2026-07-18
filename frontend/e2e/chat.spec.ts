import { expect, test, type Page } from '@playwright/test'

import { installMockWebSocketBackend } from './mock-web-socket'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../playwright/resolved-profile'

let resolvedProfile: ResolvedProfile

test.describe.configure({ mode: 'serial' })

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({}, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'mocked-e2e')
  if (reason !== null) {
    test.skip(true, reason)
  }
})

const openChatWithResponse = async (
  page: Page,
  response: { type: 'text'; response: string } | { type: 'error'; status: number; detail: string },
) => {
  await installMockWebSocketBackend(page, {
    textFrames: [{ kind: 'text', data: JSON.stringify(response) }],
    binaryFrames: [],
  })
  await page.goto('/')
}

test('ユーザーが送信したメッセージとモック応答がチャット画面に表示される', async ({ page }) => {
  await openChatWithResponse(page, { type: 'text', response: 'こんにちは、調子はどう？' })

  const input = page.getByLabel('メッセージ')
  await input.fill('こんにちは')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('こんにちは', { exact: true })).toBeVisible()
  await expect(page.getByText('こんにちは、調子はどう？')).toBeVisible()
  await expect(input).toHaveValue('')
})

test('モックBEがエラーを返した場合にエラーメッセージが表示される', async ({ page }) => {
  await openChatWithResponse(page, { type: 'error', status: 500, detail: 'backend error' })

  await page.getByLabel('メッセージ').fill('テスト')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('応答の取得に失敗しました。')).toBeVisible()
})
