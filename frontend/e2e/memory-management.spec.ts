import { expect, test, type Page } from '@playwright/test'

import { installMockWebSocketBackend } from './mock-web-socket'
import { installMockUiBootstrap } from './mock-ui-bootstrap'
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

const MEMORY_ID = '00000000-0000-4000-8000-000000000012'
const RECORD_ID = '10000000-0000-4000-8000-000000000012'

const installMemoryBackend = async (page: Page) => {
  let memories = [{
    id: MEMORY_ID,
    character_id: 'miori',
    provider_id: 'core',
    memory_kind: 'SEMANTIC',
    memory_type: 'USER_PREFERENCE',
    normalized_text: 'ユーザーは紅茶を好む',
    effective_at: '2026-08-21T00:00:00.000000Z',
    status: 'ACTIVE',
    content_version: 1,
    index_pending: true,
  }]
  const records = [{
    id: RECORD_ID,
    character_id: 'miori',
    provider_id: 'temporary:recipe',
    source_ref: 'recipe-12',
    record_type: 'RECIPE',
    structured_value: '{"name":"カレー"}',
    effective_at: '2026-08-20T00:00:00.000000Z',
    updated_at: '2026-08-22T00:00:00.000000Z',
  }]
  await installMockWebSocketBackend(page, { textFrames: [], binaryFrames: [] })
  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.includes('/persona-memories')) {
      if (request.method() === 'DELETE') memories = []
      await route.fulfill({
        status: request.method() === 'DELETE' ? 204 : 200,
        contentType: 'application/json',
        body: request.method() === 'DELETE' ? '' : JSON.stringify(memories),
      })
      return
    }
    if (url.pathname.includes('/temporary-records/temporary:recipe')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(records) })
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  })
  await installMockUiBootstrap(page)
}

test('記憶管理入口から別groupを表示し明示削除後すぐ一覧から除外する', async ({ page }) => {
  await installMemoryBackend(page)
  await page.goto('/')

  await page.getByRole('button', { name: '記憶管理' }).click()
  await expect(page.getByRole('region', { name: '記憶管理' }))
    .toHaveCSS('background-color', 'rgb(16, 13, 23)')
  await expect(page.getByRole('heading', { name: '人格記憶' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '暫定記録' })).toBeVisible()
  await expect(page.getByText('ユーザーは紅茶を好む')).toBeVisible()
  await expect(page.getByText('{"name":"カレー"}')).toBeVisible()
  await expect(page.getByText('USER_PREFERENCE')).toBeVisible()
  await expect(page.getByText('2026-08-20T00:00:00.000000Z')).toBeVisible()
  await expect(page.getByText('ACTIVE')).toBeVisible()
  await expect(page.getByText('RECIPE')).toBeVisible()
  await expect(page.getByText('2026-08-21T00:00:00.000000Z')).toBeVisible()
  await expect(page.getByText('2026-08-22T00:00:00.000000Z')).toBeVisible()

  await page.getByRole('button', { name: `削除 ${MEMORY_ID}` }).click()
  await page.getByRole('dialog').getByRole('button', { name: '完全に削除' }).click()
  await expect(page.getByText('ユーザーは紅茶を好む')).toHaveCount(0)
})
