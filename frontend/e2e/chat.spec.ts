import { expect, test, type Page } from '@playwright/test'

import { installMockWebSocketBackend } from './mock-web-socket'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from './resolved-profile'

let resolvedProfile: ResolvedProfile

test.describe.configure({ mode: 'serial' })

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({}, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
})

const installTextWebSocketBackend = async (
  page: Page,
  response: { type: 'text'; response: string } | { type: 'error'; status: number; detail: string },
) => {
  await installMockWebSocketBackend(page, {
    textFrames: [{ kind: 'text', data: JSON.stringify(response) }],
    binaryFrames: [],
  })
}

const skipWithoutChatCapability = () => {
  const reason = getCapabilitySkipReason(resolvedProfile, ['mocked-e2e', 'text-chat-real'])
  if (reason !== null) {
    test.skip(true, reason)
  }
}

const expectRealMioriResponse = async (page: Page) => {
  const mioriMessage = page.locator('article.message').nth(1)
  await expect(mioriMessage).toBeVisible()
  await expect(mioriMessage.locator('.speaker')).toHaveText('光織')
  await expect(mioriMessage.locator('p')).not.toHaveText('')
  await expect(mioriMessage.locator('p')).not.toHaveText('応答の取得に失敗しました。')
}

const openChat = async (
  page: Page,
  mockResponse: { type: 'text'; response: string } | { type: 'error'; status: number; detail: string },
) => {
  skipWithoutChatCapability()

  if (resolvedProfile.dependencies.backend.mode === 'mock') {
    await installTextWebSocketBackend(page, mockResponse)
  }

  await page.goto('/')
}

test('ユーザーが送信したメッセージと光織の応答がチャット画面に表示される', async ({ page }) => {
  await openChat(page, { type: 'text', response: 'こんにちは、調子はどう？' })

  const input = page.getByLabel('メッセージ')
  await input.fill('こんにちは')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('こんにちは', { exact: true })).toBeVisible()
  if (resolvedProfile.dependencies.backend.mode === 'mock') {
    await expect(page.getByText('こんにちは、調子はどう？')).toBeVisible()
  } else {
    await expectRealMioriResponse(page)
  }
  await expect(input).toHaveValue('')
})

test('BEがエラーを返した場合にエラーメッセージが表示される', async ({ page }) => {
  test.skip(
    resolvedProfile.dependencies.backend.mode === 'real',
    'real backend mode cannot inject a backend error response',
  )
  await openChat(page, {
    type: 'error',
    status: 500,
    detail: 'backend error',
  })

  await page.getByLabel('メッセージ').fill('テスト')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('応答の取得に失敗しました。')).toBeVisible()
})
