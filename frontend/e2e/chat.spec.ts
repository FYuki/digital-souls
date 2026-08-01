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
  response: { character: string; response: string },
) => {
  await installMockWebSocketBackend(page, {
    textFrames: [],
    binaryFrames: [],
  })
  await page.route('**/api/chat', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(response) })
  })
  await page.goto('/')
}

const readObservedConversationIds = async (page: Page, character: string): Promise<string[]> => {
  return page.evaluate((selectedCharacter) => {
    const urls = (window as unknown as { __mockWebSocketUrls: string[] }).__mockWebSocketUrls
    return urls.filter((value) => {
      const url = new URL(value)
      return url.pathname === `/ws/${selectedCharacter}`
        && url.searchParams.has('conversation_id')
    }).map((value) => {
      const conversationId = new URL(value).searchParams.get('conversation_id')
      if (conversationId === null) throw new Error('Conversation ID was not observed')
      return conversationId
    })
  }, character)
}

test('ユーザーが送信したメッセージとモック応答がチャット画面に表示される', async ({ page }) => {
  await openChatWithResponse(page, { character: 'miori', response: 'こんにちは、調子はどう？' })

  const input = page.getByLabel('メッセージ')
  await input.fill('こんにちは')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('こんにちは', { exact: true })).toBeVisible()
  await expect(page.getByText('こんにちは、調子はどう？')).toBeVisible()
  await expect(input).toHaveValue('')
})

test('モックBEがエラーを返した場合にエラーメッセージが表示される', async ({ page }) => {
  await installMockWebSocketBackend(page, { textFrames: [], binaryFrames: [] })
  await page.route('**/api/chat', async (route) => {
    await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"backend error"}' })
  })
  await page.goto('/')

  await page.getByLabel('メッセージ').fill('テスト')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('応答の取得に失敗しました。')).toBeVisible()
})

test('利用者がAからBへ切り替えてAへ戻すとcharacter別UUIDv4をHTTPとvoiceで利用する', async ({ page }) => {
  let requestBodies: Array<Record<string, string>> = []
  await installMockWebSocketBackend(page, { textFrames: [], binaryFrames: [] })
  await page.route('**/api/chat', async (route) => {
    const body = route.request().postDataJSON() as Record<string, string>
    requestBodies = [...requestBodies, body]
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ character: body.character, response: `応答${requestBodies.length}` }),
    })
  })
  await page.goto('/')

  for (const [index, message] of ['一回目', '二回目'].entries()) {
    await page.getByLabel('メッセージ').fill(message)
    await page.getByRole('button', { name: '送信' }).click()
    await expect(page.getByText(`応答${index + 1}`)).toBeVisible()
  }
  await expect.poll(async () => (await readObservedConversationIds(page, 'miori')).length).toBe(1)
  const [conversationIdA] = await readObservedConversationIds(page, 'miori')

  await page.getByLabel('キャラクターID').fill('mock-character-b')
  await page.getByRole('button', { name: '切り替え' }).click()
  await expect.poll(
    async () => (await readObservedConversationIds(page, 'mock-character-b')).length,
  ).toBe(1)
  await page.getByLabel('メッセージ').fill('Bへの質問')
  await page.getByRole('button', { name: '送信' }).click()
  await expect(page.getByText('応答3')).toBeVisible()
  const [conversationIdB] = await readObservedConversationIds(page, 'mock-character-b')

  await page.getByLabel('キャラクターID').fill('miori')
  await page.getByRole('button', { name: '切り替え' }).click()
  await expect.poll(async () => (await readObservedConversationIds(page, 'miori')).length).toBe(2)
  await page.getByLabel('メッセージ').fill('Aへの再質問')
  await page.getByRole('button', { name: '送信' }).click()
  await expect(page.getByText('応答4')).toBeVisible()
  const returnedConversationIdsA = await readObservedConversationIds(page, 'miori')

  expect(requestBodies.map((body) => body.character)).toEqual([
    'miori', 'miori', 'mock-character-b', 'miori',
  ])
  expect(requestBodies.map((body) => body.conversation_id)).toEqual([
    conversationIdA,
    conversationIdA,
    conversationIdB,
    conversationIdA,
  ])
  expect(returnedConversationIdsA).toEqual([conversationIdA, conversationIdA])
  expect(conversationIdB).not.toBe(conversationIdA)
  expect(conversationIdA).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
  )
  expect(conversationIdB).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
  )
})
