import { expect, test, type Page, type TestInfo } from '@playwright/test'

import { installMockWebSocketBackend } from './mock-web-socket'
import {
  resolveChatBackend,
  shouldSkipRealChatBackend,
  type ChatBackend,
} from './chat-backend'

let chatBackend: ChatBackend

test.describe.configure({ mode: 'serial' })

test.beforeAll(() => {
  chatBackend = resolveChatBackend()
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

const attachBackendModeEvidence = async (testInfo: TestInfo) => {
  testInfo.annotations.push({
    type: 'chat-backend',
    description: chatBackend.mode,
  })
  await testInfo.attach('chat-backend.json', {
    body: JSON.stringify(chatBackend, null, 2),
    contentType: 'application/json',
  })
}

const skipRealChatWithoutBackendOrigin = () => {
  test.skip(
    shouldSkipRealChatBackend(chatBackend),
    'CHAT_E2E_BACKEND=real requires CHAT_E2E_BACKEND_ORIGIN',
  )
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
  testInfo: TestInfo,
  mockResponse: { type: 'text'; response: string } | { type: 'error'; status: number; detail: string },
) => {
  skipRealChatWithoutBackendOrigin()
  await attachBackendModeEvidence(testInfo)

  if (chatBackend.mode === 'mock') {
    await installTextWebSocketBackend(page, mockResponse)
  }

  await page.goto('/')
}

test('ユーザーが送信したメッセージと光織の応答がチャット画面に表示される', async ({ page }, testInfo) => {
  await openChat(page, testInfo, { type: 'text', response: 'こんにちは、調子はどう？' })

  const input = page.getByLabel('メッセージ')
  await input.fill('こんにちは')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('こんにちは', { exact: true })).toBeVisible()
  if (chatBackend.mode === 'mock') {
    await expect(page.getByText('こんにちは、調子はどう？')).toBeVisible()
  } else {
    await expectRealMioriResponse(page)
  }
  await expect(input).toHaveValue('')
})

test('BEがエラーを返した場合にエラーメッセージが表示される', async ({ page }, testInfo) => {
  test.skip(chatBackend.mode === 'real', 'real backend mode cannot inject a backend error response')
  await openChat(page, testInfo, {
    type: 'error',
    status: 500,
    detail: 'backend error',
  })

  await page.getByLabel('メッセージ').fill('テスト')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('応答の取得に失敗しました。')).toBeVisible()
})
