import { expect, test } from '@playwright/test'
import { installMockUiBootstrap } from './mock-ui-bootstrap'
import { installMockLiveKit } from './mock-livekit'
import { attachProfileEvidence, getCapabilitySkipReason, readResolvedProfile, type ResolvedProfile } from '../playwright/resolved-profile'

let resolvedProfile: ResolvedProfile
test.beforeAll(async () => { resolvedProfile = await readResolvedProfile() })
test.beforeEach(async ({}, info) => {
  await attachProfileEvidence(info, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'mocked-e2e')
  if (reason !== null) test.skip(true, reason)
})

const conversationId = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const requestId = 'c98d6c65-1ae9-4d6f-a8c8-d59b0ad09011'

for (const scenario of [
  { choice: 'always', label: '常に承認する', width: 1280, voice: false },
  { choice: 'once', label: '一度承認する', width: 320, voice: false },
  { choice: 'reject', label: '拒否する', width: 1280, voice: false },
  { choice: 'once', label: '一度承認する', width: 1280, voice: true },
].flatMap(item => [{ ...item, admin: false }, ...(item.choice === 'once' ? [{ ...item, admin: true }] : [])])) {
  test(`承認UI ${scenario.label} ${scenario.width}px voice=${scenario.voice} admin=${scenario.admin}`, async ({ page }, info) => {
    await page.setViewportSize({ width: scenario.width, height: 1000 })
    await page.route('**/api/**', async route => { await route.fulfill({ json: [] }) })
    await installMockUiBootstrap(page)
    if (scenario.voice) await installMockLiveKit(page, { response: '画面で操作を承認してください。' })
    let created = false
    const conversation = { character_id: 'miori', conversation_id: conversationId, title: '承認テスト',
      created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z', archived_at: null }
    await page.route('**/api/characters/miori/**', async route => {
      if (new URL(route.request().url()).pathname.endsWith('/turns')) {
        await route.fulfill({ json: [] }); return
      }
      if (route.request().method() === 'POST') created = true
      await route.fulfill({ json: route.request().method() === 'POST' ? conversation : created ? [conversation] : [] })
    })
    let waiting = false
    let saved: string | null = null
    let continued = 0
    let stops = 0
    await page.route('**/api/tool-use/stop', async route => {
      stops += 1; waiting = false
      await route.fulfill({ json: { state: 'stopped' } })
    })
    const queueItem = () => ({ id: requestId, choice: saved, operation_group: 'high_impact', scene: 'conversation',
      connection_id: 'test', character_id: 'miori', session_id: conversationId, created_at: Date.now() / 1000,
      wait_until: Date.now() / 1000 + 600, waiting, once_reserved: saved === 'once', connection_available: true,
      preview: { connection: 'テスト文書MCP', operation: 'write_document', target: '破棄可能な文書', arguments: { text: 'テスト更新' } },
    })
    await page.route('**/api/tool-use/status/**', async route => { await route.fulfill({ json: {
      state: waiting ? 'waiting' : 'idle', sources: [], ...(waiting ? { confirmation_id: requestId } : {}),
    } }) })
    await page.route('**/api/chat', async route => {
      waiting = true
      await route.fulfill({ json: { character: 'miori', turn: { kind: 'content', turn_id: requestId,
        user_content: '文書を変更して', assistant_content: '画面で操作を承認してください。' } } })
    })
    await page.route('**/api/addon-actions/**', async route => {
      const path = new URL(route.request().url()).pathname
      if (path.endsWith('/permissions')) {
        await route.fulfill({ json: { permissions: [{ connection_id: 'test', connection_token: 'token',
          connection_label: 'テスト文書MCP', operation_group: 'high_impact', scene: 'conversation',
          permission: 'unapproved', remaining: 0, reserved: 0 }] } })
      } else if (path.endsWith('/answer')) {
        const body = route.request().postDataJSON()
        expect(body).toEqual(scenario.admin ? { choice: scenario.choice } : { character: 'miori', session_id: conversationId, choice: scenario.choice })
        saved = body.choice
        await route.fulfill({ json: scenario.admin ? { request: queueItem() } : {} })
      } else if (path.endsWith('/continue')) {
        expect(saved).toBe(scenario.choice)
        if (!scenario.admin) {
          const body = route.request().postDataJSON()
          expect(body).toEqual({ character: 'miori', conversation_id: conversationId,
            ...(scenario.voice ? { voice_session_id: '20000000-0000-4000-8000-000000000010' } : {}) })
        }
        continued += 1
        waiting = false
        await route.fulfill({ json: scenario.voice ? { state: 'voice_started' } : { character: 'miori', turn: {
          kind: 'content', turn_id: 'a98d6c65-1ae9-4d6f-a8c8-d59b0ad09012', user_content: `画面で${scenario.label}`,
          assistant_content: scenario.choice === 'reject' ? '操作を拒否しました。' : '文書を変更しました。',
        } } })
      } else await route.fulfill({ json: { requests: waiting ? [queueItem()] : [], next_cursor: null } })
    })
    await page.goto('/')
    if (scenario.width === 320) await page.getByRole('button', { name: 'サイドバーを開く' }).click()
    await page.getByRole('button', { name: '新規スレッド（光織）' }).click()
    if (scenario.voice) {
      await page.getByRole('button', { name: 'マイクをオンにする' }).click()
      await page.waitForFunction(() => Boolean((window as unknown as { __mockLiveKit?: unknown }).__mockLiveKit))
      await page.evaluate(async () => {
        await (window as unknown as { __mockLiveKit: { submitUtterance: () => Promise<void> } }).__mockLiveKit.submitUtterance()
      })
      waiting = true
    } else {
      await page.getByLabel('メッセージ').fill('文書を変更して')
      await page.getByRole('button', { name: '送信' }).click()
    }
    await expect(page.getByRole('region', { name: '外部操作の承認' })).toBeVisible()
    if (scenario.admin) {
      const opener = page.getByRole('button', { name: /Addon \/ 連携/ })
      if (!(await opener.isVisible())) await page.getByRole('button', { name: 'サイドバーを開く' }).click()
      await opener.click()
      await expect(page.getByRole('region', { name: '確認キュー・承認設定' })).toContainText('元の操作は待機中です')
      expect(stops).toBe(0)
    }
    for (const name of ['常に承認する', '一度承認する', '拒否する']) await expect(page.getByRole('button', { name, exact: true })).toBeVisible()
    expect(saved).toBeNull()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    if (scenario.width === 320 && !scenario.admin) {
      const send = await page.getByRole('button', { name: '送信', exact: true }).boundingBox()
      const mic = await page.getByRole('button', { name: 'マイクをオンにする' }).boundingBox()
      expect(send).not.toBeNull()
      expect(mic).not.toBeNull()
      expect(send!.x + send!.width).toBeLessThanOrEqual(mic!.x)
    }
    const screenshot = info.outputPath('approval-ui.png')
    await page.screenshot({ path: screenshot })
    await info.attach('approval-ui', { path: screenshot, contentType: 'image/png' })
    await page.getByRole('button', { name: scenario.label, exact: true }).click()
    await expect.poll(() => continued).toBe(1)
    if (scenario.admin) {
      await page.getByRole('button', { name: 'チャットへ戻る', exact: true }).click()
      if (scenario.width === 320) await page.getByRole('button', { name: 'サイドバーを閉じる' }).click()
    }
    if (!scenario.voice) await expect(page.getByText(scenario.choice === 'reject' ? '操作を拒否しました。' : '文書を変更しました。', { exact: true })).toBeVisible()
  })
}
