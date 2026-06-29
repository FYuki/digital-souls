import { expect, test } from '@playwright/test'

test('ユーザーが送信したメッセージと光織の応答がチャット画面に表示される', async ({ page }) => {
  await page.route('**/api/chat', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ character: 'miori', response: 'こんにちは、調子はどう？' }),
    })
  })

  await page.goto('/')

  const input = page.getByLabel('メッセージ')
  await input.fill('こんにちは')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('こんにちは', { exact: true })).toBeVisible()
  await expect(page.getByText('こんにちは、調子はどう？')).toBeVisible()
  await expect(input).toHaveValue('')
})

test('BEがエラーを返した場合にエラーメッセージが表示される', async ({ page }) => {
  await page.route('**/api/chat', async (route) => {
    await route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
  })

  await page.goto('/')

  await page.getByLabel('メッセージ').fill('テスト')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('応答の取得に失敗しました。')).toBeVisible()
})
