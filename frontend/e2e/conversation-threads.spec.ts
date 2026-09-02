import { expect, test, type Page } from '@playwright/test'

import { installMockWebSocketBackend } from './mock-web-socket'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../playwright/resolved-profile'

let resolvedProfile: ResolvedProfile

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({}, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'mocked-e2e')
  if (reason !== null) test.skip(true, reason)
})

const ACTIVE_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const ARCHIVED_ID = '62f217d0-9b14-40f8-8df3-dd6f4a7dc758'
const MASKED_USER = '連絡先は[REDACTED]です'
const MASKED_ASSISTANT = '保存済みの回答です'
const PRIVACY_SKIPPED_ID = 'a71d37fc-d65c-48ed-80a6-52968ea93628'

type LifecycleOptions = {
  historyTurns?: unknown[]
}

const conversation = (conversationId: string, archivedAt: string | null) => ({
  character_id: 'miori',
  conversation_id: conversationId,
  created_at: '2026-08-01T12:00:00.000000Z',
  updated_at: '2026-08-01T12:01:00.000000Z',
  archived_at: archivedAt,
  title: conversationId,
})

const installLifecycleBackend = async (page: Page, options: LifecycleOptions = {}) => {
  let active = [conversation(ACTIVE_ID, null)]
  let archived = [conversation(ARCHIVED_ID, '2026-08-01T13:00:00.000000Z')]
  const chatRequests: Array<Record<string, string>> = []

  await installMockWebSocketBackend(page, { textFrames: [], binaryFrames: [] })
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()
    const body = request.postDataJSON() as Record<string, string> | null
    let response: unknown

    if (url.pathname === '/api/chat') {
      if (body !== null) chatRequests.push(body)
      response = {
        character: 'miori',
        turn: {
          kind: 'content',
          turn_id: '9e70795d-e5d5-431d-baa2-67f884403010',
          user_content: MASKED_USER,
          assistant_content: MASKED_ASSISTANT,
        },
      }
    } else if (method === 'POST' && url.pathname.endsWith('/archive')) {
      const item = active.find((candidate) => url.pathname.includes(candidate.conversation_id))
      active = active.filter((candidate) => candidate !== item)
      const transitioned = item === undefined
        ? undefined
        : { ...item, archived_at: '2026-08-01T13:30:00.000000Z' }
      if (transitioned !== undefined) archived = [...archived, transitioned]
      response = transitioned
    } else if (method === 'POST' && url.pathname.endsWith('/unarchive')) {
      const item = archived.find((candidate) => url.pathname.includes(candidate.conversation_id))
      archived = archived.filter((candidate) => candidate !== item)
      const transitioned = item === undefined ? undefined : { ...item, archived_at: null }
      if (transitioned !== undefined) active = [...active, transitioned]
      response = transitioned
    } else if (method === 'DELETE') {
      archived = archived.filter((candidate) => !url.pathname.includes(candidate.conversation_id))
      response = null
    } else if (url.pathname.includes('/turns') || url.pathname.endsWith(`/${ACTIVE_ID}`)) {
      response = options.historyTurns ?? [{
        kind: 'content',
        turn_id: '9e70795d-e5d5-431d-baa2-67f884403010',
        user_content: MASKED_USER,
        assistant_content: MASKED_ASSISTANT,
      }]
    } else if (url.pathname.includes('archived')) {
      response = archived
    } else if (method === 'POST') {
      const created = conversation('c7b12e47-a2cf-4af7-b503-e4f447ee03ad', null)
      active = [created, ...active]
      response = created
    } else {
      response = url.pathname.includes('/characters/akira/') ? [] : active
    }

    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: response === null ? '' : JSON.stringify(response),
    })
  })
  return { chatRequests }
}

test('既存スレッドを選ぶと保存済み履歴を表示し同じIDで再開する', async ({ page }) => {
  const backend = await installLifecycleBackend(page)
  await page.goto('/')

  await page.getByRole('button', { name: ACTIVE_ID, exact: true }).click()

  await expect(page.getByText(MASKED_USER, { exact: true })).toBeVisible()
  await expect(page.getByText(MASKED_ASSISTANT, { exact: true })).toBeVisible()
  await page.getByLabel('メッセージ').fill('保存前の原文')
  await page.getByRole('button', { name: '送信' }).click()
  await expect.poll(() => backend.chatRequests.length).toBe(1)
  expect(backend.chatRequests[0]?.conversation_id).toBe(ACTIVE_ID)
  await expect(page.getByText('保存前の原文', { exact: true })).toHaveCount(0)
})

test('新規スレッドは既存選択と混同せずBackendが発行したIDへ切り替える', async ({ page }) => {
  const backend = await installLifecycleBackend(page)
  await page.goto('/')

  await page.getByRole('button', { name: '新規スレッド' }).click()
  await page.getByLabel('メッセージ').fill('新しい会話')
  await page.getByRole('button', { name: '送信' }).click()

  await expect.poll(() => backend.chatRequests.length).toBe(1)
  expect(backend.chatRequests[0]?.conversation_id).toBe('c7b12e47-a2cf-4af7-b503-e4f447ee03ad')
})

test('hard delete確認は削除範囲・復元不能・保証対象外を明示する', async ({ page }) => {
  await installLifecycleBackend(page)
  await page.goto('/')

  await page.getByRole('button', { name: 'アーカイブ済み' }).click()
  await page.getByRole('button', { name: new RegExp(`削除.*${ARCHIVED_ID}`) }).click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText(ARCHIVED_ID)
  await expect(dialog).toContainText('短期会話履歴')
  await expect(dialog).toContainText('復元できません')
  await expect(dialog).toContainText('RAG長期記憶は削除されません')
  await expect(dialog).toContainText('backup')
  await expect(dialog).toContainText('snapshot')
  await expect(dialog).toContainText('ファイルシステム上の複製')
  await expect(dialog).toContainText('消去を保証しません')
})

test('character切替時に前characterの一覧・履歴・選択を即時に表示しない', async ({ page }) => {
  await installLifecycleBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: ACTIVE_ID, exact: true }).click()
  await expect(page.getByText(MASKED_ASSISTANT, { exact: true })).toBeVisible()

  await page.getByLabel('キャラクターID').fill('akira')
  await page.getByRole('button', { name: '切り替え' }).click()

  await expect(page.getByText(MASKED_ASSISTANT, { exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: new RegExp(ACTIVE_ID) })).toHaveCount(0)
  const selected = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:akira'))
  expect(selected).not.toBe(ACTIVE_ID)
})

test('archive成功後はactive一覧と選択状態から除去しarchived一覧へ移す', async ({ page }) => {
  await installLifecycleBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: ACTIVE_ID, exact: true }).click()

  await page.getByRole('button', { name: new RegExp(`アーカイブ.*${ACTIVE_ID}`) }).click()

  await expect(page.getByRole('button', { name: new RegExp(ACTIVE_ID) })).toHaveCount(0)
  await expect(page.getByText(MASKED_ASSISTANT, { exact: true })).toHaveCount(0)
  const selected = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:miori'))
  expect(selected).not.toBe(ACTIVE_ID)
  await page.getByRole('button', { name: 'アーカイブ済み' }).click()
  await expect(page.getByRole('button', { name: ARCHIVED_ID, exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: ACTIVE_ID, exact: true })).toBeVisible()
  await expect(page.getByText('履歴は削除されずSQLiteに保持されます', { exact: false })).toBeVisible()
})

test('unarchive成功後は同じIDをactive一覧へ戻して再開できる', async ({ page }) => {
  await installLifecycleBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: 'アーカイブ済み' }).click()

  await page.getByRole('button', { name: new RegExp(`復元.*${ARCHIVED_ID}`) }).click()

  await page.getByRole('button', { name: 'アクティブ' }).click()
  await page.getByRole('button', { name: ARCHIVED_ID, exact: true }).click()
  const selected = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:miori'))
  expect(selected).toBe(ARCHIVED_ID)
})

test('hard delete成功後は一覧・履歴・選択ID・localStorageから除去する', async ({ page }) => {
  await page.addInitScript((conversationId) => {
    localStorage.setItem('digital-souls:conversation:miori', conversationId)
  }, ARCHIVED_ID)
  await installLifecycleBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: 'アーカイブ済み' }).click()
  await page.getByRole('button', { name: new RegExp(`削除.*${ARCHIVED_ID}`) }).click()

  await page.getByRole('dialog').getByRole('button', { name: '完全に削除' }).click()

  await expect(page.getByText(ARCHIVED_ID, { exact: false })).toHaveCount(0)
  await expect(page.getByText(MASKED_ASSISTANT, { exact: true })).toHaveCount(0)
  const selected = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:miori'))
  expect(selected).toBeNull()
})

test('privacy_skipped履歴は本文を作らずreason metadataを表示する', async ({ page }) => {
  await installLifecycleBackend(page, {
    historyTurns: [{
      kind: 'privacy_skipped',
      turn_id: PRIVACY_SKIPPED_ID,
      reason_code: 'STORAGE_OPT_OUT',
      sanitizer_version: 'history-sanitizer-v1',
      policy_version: 'privacy-policy-v1',
    }],
  })
  await page.goto('/')

  await page.getByRole('button', { name: ACTIVE_ID, exact: true }).click()

  await expect(page.getByText('STORAGE_OPT_OUT', { exact: false })).toBeVisible()
  await expect(page.locator(`[data-turn-id="${PRIVACY_SKIPPED_ID}"]`)).not.toContainText(SENSITIVE_RAW_TEXT)
})

const SENSITIVE_RAW_TEXT = '復元してはいけない原文'

test('character切替後に到着した旧characterの遅延応答を破棄する', async ({ page }) => {
  let releaseMiori: (() => void) | undefined
  let mioriRequests = 0
  let akiraRequests = 0
  const mioriReleased = new Promise<void>((resolve) => {
    releaseMiori = resolve
  })
  await installMockWebSocketBackend(page, { textFrames: [], binaryFrames: [] })
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname.includes('/characters/miori/') && !pathname.includes('/turns')) {
      mioriRequests += 1
      await mioriReleased
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([conversation(ACTIVE_ID, null)]) })
      return
    }
    if (pathname.includes('/characters/akira/')) akiraRequests += 1
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  })
  await page.goto('/')
  await expect.poll(() => mioriRequests).toBeGreaterThan(0)
  await page.getByLabel('キャラクターID').fill('akira')
  await page.getByRole('button', { name: '切り替え' }).click()
  await expect.poll(() => akiraRequests).toBeGreaterThan(0)

  if (releaseMiori === undefined) throw new Error('miori response resolver was not initialized')
  releaseMiori()

  await expect(page.getByRole('button', { name: new RegExp(ACTIVE_ID) })).toHaveCount(0)
  const selected = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:akira'))
  expect(selected).not.toBe(ACTIVE_ID)
})
