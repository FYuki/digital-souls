import { expect, test, type Page } from '@playwright/test'

import { installMockWebSocketBackend } from './mock-web-socket'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../playwright/resolved-profile'

let resolvedProfile: ResolvedProfile
const CONVERSATION_IDS: Record<string, string> = {
  miori: 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010',
  'mock-character-b': '62f217d0-9b14-40f8-8df3-dd6f4a7dc758',
}

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

const installConversationBackend = async (page: Page) => {
  let createdCharacters: string[] = []
  await page.route('**/api/characters/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const character = url.pathname.split('/')[3]
    const conversationId = CONVERSATION_IDS[character]
    if (conversationId === undefined) throw new Error(`Unexpected character: ${character}`)
    if (url.pathname.endsWith('/turns')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
      return
    }
    if (request.method() === 'POST') createdCharacters = [...createdCharacters, character]
    const conversations = createdCharacters.includes(character) ? [{
      character_id: character,
      conversation_id: conversationId,
      created_at: '2026-08-01T12:00:00.000000Z',
      updated_at: '2026-08-01T12:00:00.000000Z',
      archived_at: null,
    }] : []
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(request.method() === 'POST' ? conversations[0] : conversations),
    })
  })
}

const openNewChat = async (page: Page, assistantContent: string) => {
  await installMockWebSocketBackend(page, {
    textFrames: [],
    binaryFrames: [],
  })
  await installConversationBackend(page)
  await page.route('**/api/chat', async (route) => {
    const body = route.request().postDataJSON() as Record<string, string>
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      character: body.character,
      turn: {
        kind: 'content',
        turn_id: '9e70795d-e5d5-431d-baa2-67f884403010',
        user_content: body.message,
        assistant_content: assistantContent,
      },
    }) })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '新規スレッド' }).click()
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
  await openNewChat(page, 'こんにちは、調子はどう？')

  const input = page.getByLabel('メッセージ')
  await input.fill('こんにちは')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('こんにちは', { exact: true })).toBeVisible()
  await expect(page.getByText('こんにちは、調子はどう？')).toBeVisible()
  await expect(input).toHaveValue('')
})

test('スレッド機能を表示したときも既存の背景とレイアウトを維持する', async ({ page }) => {
  await installConversationBackend(page)
  await page.goto('/')

  await expect(page.locator('.app-shell')).toHaveCSS('background-image', /^linear-gradient/)
  await expect(page.locator('.app-shell')).toHaveCSS('align-items', 'stretch')
  await expect(page.locator('.chat-panel')).toHaveCSS(
    'box-shadow',
    /rgba\(69, 39, 33, 0\.12\) 0px 18px 42px 0px/,
  )
})

test('モックBEがエラーを返した場合にエラーメッセージが表示される', async ({ page }) => {
  await installMockWebSocketBackend(page, { textFrames: [], binaryFrames: [] })
  await installConversationBackend(page)
  await page.route('**/api/chat', async (route) => {
    await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"backend error"}' })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '新規スレッド' }).click()

  await page.getByLabel('メッセージ').fill('テスト')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('応答の取得に失敗しました。')).toBeVisible()
})

test('利用者がAからBへ切り替えてAへ戻すとcharacter別UUIDv4をHTTPとvoiceで利用する', async ({ page }) => {
  let requestBodies: Array<Record<string, string>> = []
  await installMockWebSocketBackend(page, { textFrames: [], binaryFrames: [] })
  await installConversationBackend(page)
  await page.route('**/api/chat', async (route) => {
    const body = route.request().postDataJSON() as Record<string, string>
    requestBodies = [...requestBodies, body]
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        character: body.character,
        turn: {
          kind: 'content',
          turn_id: `9e70795d-e5d5-431d-baa2-67f88440301${requestBodies.length}`,
          user_content: body.message,
          assistant_content: `応答${requestBodies.length}`,
        },
      }),
    })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '新規スレッド' }).click()

  for (const [index, message] of ['一回目', '二回目'].entries()) {
    await page.getByLabel('メッセージ').fill(message)
    await page.getByRole('button', { name: '送信' }).click()
    await expect(page.getByText(`応答${index + 1}`)).toBeVisible()
  }
  await expect.poll(async () => (await readObservedConversationIds(page, 'miori')).length).toBe(1)
  const [conversationIdA] = await readObservedConversationIds(page, 'miori')

  await page.getByLabel('キャラクターID').fill('mock-character-b')
  await page.getByRole('button', { name: '切り替え' }).click()
  await page.getByRole('button', { name: '新規スレッド' }).click()
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
