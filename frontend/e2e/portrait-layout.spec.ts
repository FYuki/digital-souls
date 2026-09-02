import { expect, test, type Page, type TestInfo } from '@playwright/test'
import { readFileSync } from 'node:fs'

import { installMockUiBootstrap } from './mock-ui-bootstrap'
import { installMockWebSocketBackend } from './mock-web-socket'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../playwright/resolved-profile'

let resolvedProfile: ResolvedProfile

const CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const PORTRAIT_URL = '/api/characters/miori/assets/standing/default.png'
const portrait = readFileSync(new URL('../../characters/miori/assets/standing/default.png', import.meta.url))

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({}, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'mocked-e2e')
  if (reason !== null) test.skip(true, reason)
})

const conversation = {
  character_id: 'miori',
  conversation_id: CONVERSATION_ID,
  created_at: '2026-08-01T12:00:00.000000Z',
  updated_at: '2026-08-01T12:01:00.000000Z',
  archived_at: null,
  title: '夕暮れの相談',
}

const installPortraitBackend = async (
  page: Page,
  image: 'available' | 'missing' | 'failed' = 'available',
) => {
  await installMockWebSocketBackend(page, { textFrames: [], binaryFrames: [] })
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    const response = pathname.endsWith('/turns')
      ? [{
          kind: 'content',
          turn_id: '9e70795d-e5d5-431d-baa2-67f884403010',
          user_content: '今日のことを聞いてほしい',
          assistant_content: 'もちろん。ここでゆっくり聞かせて。',
        }]
      : pathname.includes('archived') ? [] : [conversation]
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(response),
    })
  })
  await installMockUiBootstrap(page, [{
    character_id: 'miori',
    display_name: '光織',
    standing_image: image === 'missing'
      ? { status: 'missing', url: null }
      : { status: 'available', url: PORTRAIT_URL },
  }])
  if (image !== 'missing') {
    await page.route(`**${PORTRAIT_URL}`, async (route) => {
      await route.fulfill(image === 'available'
        ? { status: 200, contentType: 'image/png', body: portrait }
        : { status: 404, contentType: 'application/json', body: '{}' })
    })
  }
}

const attachScreenshot = async (page: Page, testInfo: TestInfo, name: string) => {
  const path = testInfo.outputPath(`${name}.png`)
  await page.screenshot({ path, fullPage: true })
  await testInfo.attach(name, { path, contentType: 'image/png' })
}

test('PCでは右配置を初期値とし背面配置と50%設定を再読み込み後も復元する', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await installPortraitBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: '夕暮れの相談', exact: true }).click()

  const stage = page.locator('.conversation-stage')
  await expect(stage).toHaveAttribute('data-portrait-layout', 'right')
  await expect(page.getByRole('img', { name: '光織の立ち絵' })).toBeVisible()
  await attachScreenshot(page, testInfo, 'pc-right')

  await page.getByRole('button', { name: '設定' }).click()
  await page.getByRole('combobox', { name: 'PCの立ち絵配置' }).selectOption('background')
  await expect(stage).toHaveAttribute('data-portrait-layout', 'background')
  await expect(page.getByRole('combobox', { name: 'PC・履歴背面の表示範囲' })).toBeEnabled()
  await page.getByRole('combobox', { name: 'PC・履歴背面の表示範囲' }).selectOption('50')
  await expect(stage).toHaveAttribute('data-history-height', '50')
  await page.getByRole('button', { name: '設定を閉じる' }).click()
  await attachScreenshot(page, testInfo, 'pc-background-50')

  await page.reload()
  await expect(stage).toHaveAttribute('data-portrait-layout', 'background')
  await expect(stage).toHaveAttribute('data-history-height', '50')
  const geometry = await page.locator('.conversation-stage, .history-layer, .input-area')
    .evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().toJSON()))
  expect(geometry[1]?.height).toBeCloseTo((geometry[0]?.height ?? 0) * 0.5, 0)
  expect(geometry[2]?.top).toBeGreaterThanOrEqual(geometry[0]?.bottom ?? 0)
})

for (const compact of [
  { name: 'tablet', width: 768, height: 1024, percentages: ['50', '75', '100'] },
  { name: 'mobile', width: 390, height: 844, percentages: ['100', '75', '50'] },
] as const) {
  test(`${compact.name}は背面配置に固定し50・75・100%を下端基準で反映する`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: compact.width, height: compact.height })
    await installPortraitBackend(page)
    await page.goto('/')

    const stage = page.locator('.conversation-stage')
    await expect(stage).toHaveAttribute('data-portrait-layout', 'background')
    await page.getByRole('button', { name: 'サイドバーを開く' }).click()
    await page.getByRole('button', { name: '夕暮れの相談', exact: true }).click()
    await page.getByRole('button', { name: 'サイドバーを開く' }).click()
    await page.getByRole('button', { name: '設定' }).click()
    const setting = page.getByRole('combobox', { name: 'タブレット・モバイルの表示範囲' })
    for (const percent of compact.percentages) {
      await setting.selectOption(percent)
      await expect(stage).toHaveAttribute('data-history-height', percent)
      await expect(setting).toBeEnabled()
      const geometry = await page.locator('.conversation-stage, .history-layer')
        .evaluateAll((elements) => elements.map((element) => element.getBoundingClientRect().toJSON()))
      expect(geometry[1]?.height).toBeCloseTo(
        (geometry[0]?.height ?? 0) * Number(percent) / 100,
        0,
      )
      expect(geometry[1]?.bottom).toBeCloseTo(geometry[0]?.bottom ?? 0, 0)
    }
    await page.keyboard.press('Escape')
    await expect(page.getByRole('region', { name: '設定' })).toHaveCount(0)
    await page.keyboard.press('Escape')
    await expect(page.getByRole('complementary', { name: 'スレッド一覧' })).toHaveCount(0)
    await attachScreenshot(page, testInfo, compact.name)
    await page.reload()
    await expect(stage).toHaveAttribute(
      'data-history-height',
      compact.percentages[compact.percentages.length - 1],
    )
  })
}

test('Visual Viewport縮小時は一時的に100%へ変更せず入力欄を表示範囲内へ保つ', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.addInitScript(() => {
    let height = 844
    let offsetTop = 0
    const viewport = new EventTarget()
    Object.defineProperties(viewport, {
      height: { get: () => height },
      offsetTop: { get: () => offsetTop },
    })
    Object.defineProperty(window, 'visualViewport', { value: viewport })
    Object.defineProperty(window, '__setMockVisualViewport', {
      value: (nextHeight: number, nextOffsetTop: number) => {
        height = nextHeight
        offsetTop = nextOffsetTop
        viewport.dispatchEvent(new Event('resize'))
      },
    })
  })
  await installPortraitBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: 'サイドバーを開く' }).click()
  await page.getByRole('button', { name: '夕暮れの相談', exact: true }).click()

  await page.evaluate(() => {
    const setter = (window as unknown as {
      __setMockVisualViewport: (height: number, offsetTop: number) => void
    }).__setMockVisualViewport
    setter(460, 120)
  })
  const shell = page.locator('.app-shell')
  await expect(shell).toHaveCSS('height', '460px')
  await expect(shell).toHaveCSS('top', '120px')
  await expect(page.locator('.conversation-stage')).toHaveAttribute('data-history-height', '75')
  const input = await page.locator('.input-area').boundingBox()
  expect(input?.y).toBeGreaterThanOrEqual(120)
  expect((input?.y ?? 0) + (input?.height ?? 0)).toBeLessThanOrEqual(580.5)
})

for (const image of ['missing', 'failed'] as const) {
  test(`立ち絵が${image === 'missing' ? '未設定' : '読み込み失敗'}でもplaceholderで会話を継続できる`, async ({ page }) => {
    await installPortraitBackend(page, image)
    await page.goto('/')
    await expect(page.getByLabel('立ち絵未設定')).toBeVisible()
    await page.getByRole('button', { name: '夕暮れの相談', exact: true }).click()
    await expect(page.getByLabel('メッセージ')).toBeEnabled()
    await page.getByLabel('メッセージ').fill('表示失敗後も入力できる')
    await expect(page.getByRole('button', { name: '送信' })).toBeEnabled()
  })
}
