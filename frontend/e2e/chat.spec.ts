import { expect, test } from '@playwright/test'

import { installMockWebSocketBackend } from './mock-web-socket'

const installTextWebSocketBackend = async (
  page: import('@playwright/test').Page,
  response: { type: 'text'; response: string } | { type: 'error'; status: number; detail: string },
) => {
  await installMockWebSocketBackend(page, {
    textFrames: [{ kind: 'text', data: JSON.stringify(response) }],
    binaryFrames: [],
  })
}

test('ユーザーが送信したメッセージと光織の応答がチャット画面に表示される', async ({ page }) => {
  await installTextWebSocketBackend(page, { type: 'text', response: 'こんにちは、調子はどう？' })

  await page.goto('/')

  const input = page.getByLabel('メッセージ')
  await input.fill('こんにちは')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('こんにちは', { exact: true })).toBeVisible()
  await expect(page.getByText('こんにちは、調子はどう？')).toBeVisible()
  await expect(input).toHaveValue('')
})

test('BEがエラーを返した場合にエラーメッセージが表示される', async ({ page }) => {
  await installTextWebSocketBackend(page, {
    type: 'error',
    status: 500,
    detail: 'backend error',
  })

  await page.goto('/')

  await page.getByLabel('メッセージ').fill('テスト')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('応答の取得に失敗しました。')).toBeVisible()
})
