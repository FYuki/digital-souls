import { expect, test } from '@playwright/test'
import { installMockUiBootstrap } from './mock-ui-bootstrap'
import { installMockLiveKit } from './mock-livekit'
import { attachProfileEvidence, getCapabilitySkipReason, readResolvedProfile, type ResolvedProfile } from '../playwright/resolved-profile'

let profile: ResolvedProfile
test.beforeAll(async () => { profile = await readResolvedProfile() })
test.beforeEach(async ({}, info) => {
  await attachProfileEvidence(info, profile)
  const reason = getCapabilitySkipReason(profile, 'mocked-e2e')
  if (reason !== null) test.skip(true, reason)
})

const conversationId = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const requestId = 'c98d6c65-1ae9-4d6f-a8c8-d59b0ad09011'

for (const scenario of [
  { choice: 'always', label: '常に承認する', width: 1280, voice: false },
  { choice: 'once', label: '一度承認する', width: 320, voice: false },
  { choice: 'reject', label: '拒否する', width: 1280, voice: false },
  { choice: 'once', label: '一度承認する', width: 1280, voice: true },
]) {
  test(`承認UI ${scenario.label} ${scenario.width}px voice=${scenario.voice}`, async ({ page }, info) => {
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
      if (path.endsWith('/answer')) {
        const body = route.request().postDataJSON()
        expect(body).toEqual({ character: 'miori', session_id: conversationId, choice: scenario.choice })
        saved = body.choice
        await route.fulfill({ json: {} })
      } else if (path.endsWith('/continue')) {
        expect(saved).toBe(scenario.choice)
        const body = route.request().postDataJSON()
        expect(body).toEqual({ character: 'miori', conversation_id: conversationId,
          ...(scenario.voice ? { voice_session_id: '20000000-0000-4000-8000-000000000010' } : {}) })
        continued += 1
        waiting = false
        await route.fulfill({ json: scenario.voice ? { state: 'voice_started' } : { character: 'miori', turn: {
          kind: 'content', turn_id: 'a98d6c65-1ae9-4d6f-a8c8-d59b0ad09012', user_content: `画面で${scenario.label}`,
          assistant_content: scenario.choice === 'reject' ? '操作を拒否しました。' : '文書を変更しました。',
        } } })
      } else await route.fulfill({ json: { requests: [{ id: requestId, choice: saved, operation_group: 'high_impact', scene: 'conversation',
        preview: { connection: 'テスト文書MCP', operation: 'write_document', target: '破棄可能な文書', arguments: { text: 'テスト更新' } },
      }] } })
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
    for (const name of ['常に承認する', '一度承認する', '拒否する']) await expect(page.getByRole('button', { name, exact: true })).toBeVisible()
    expect(saved).toBeNull()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    if (scenario.width === 320) {
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
    if (!scenario.voice) await expect(page.getByText(scenario.choice === 'reject' ? '操作を拒否しました。' : '文書を変更しました。', { exact: true })).toBeVisible()
  })
}
