import { expect, test, type Page } from '@playwright/test'

import { hardDeleteSelectedConversation } from '../../playwright/conversation-cleanup'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../../playwright/resolved-profile'

let resolvedProfile: ResolvedProfile
const REAL_RESPONSE_TIMEOUT_MS = 90_000

test.describe.configure({ mode: 'serial' })
test.setTimeout(REAL_RESPONSE_TIMEOUT_MS + 30_000)

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({ page }, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'text-chat-real')
  if (reason !== null) test.skip(true, reason)
  await installSyntheticBrowserCapture(page)
})

test.afterEach(async ({ page }) => {
  await hardDeleteSelectedConversation(page, 'miori')
})

const installSyntheticBrowserCapture = async (page: Page) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'userActivation', {
      configurable: true,
      value: { isActive: true },
    })
    Object.defineProperty(navigator.mediaDevices, 'getDisplayMedia', {
      configurable: true,
      value: async () => {
        const canvas = document.createElement('canvas')
        canvas.width = 640
        canvas.height = 360
        const context = canvas.getContext('2d')
        if (context === null) throw new Error('synthetic canvas context is required')
        context.fillStyle = 'white'
        context.fillRect(0, 0, canvas.width, canvas.height)
        context.fillStyle = 'black'
        context.font = '24px sans-serif'
        context.fillText('PUBLIC SYNTHETIC SCREEN', 24, 40)
        context.fillStyle = 'red'
        context.fillRect(210, 110, 220, 130)
        context.fillStyle = 'white'
        context.fillText('WARNING 42', 245, 180)
        const stream = canvas.captureStream(10)
        const track = stream.getVideoTracks()[0]
        if (track === undefined) throw new Error('synthetic video track is required')
        Object.defineProperty(track, 'getSettings', {
          configurable: true,
          value: () => ({ displaySurface: 'browser' }),
        })
        Object.defineProperty(track, 'label', {
          configurable: true,
          value: '統合試験用合成タブ',
        })
        return stream
      },
    })
  })
}

test('browser共有画像を実BackendとOllama Visionで会話回答へ統合する', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新規スレッド（光織）' }).click()
  await page.getByText('画面共有の詳細').click()
  await page.getByLabel('共有する画面の種類').selectOption('browser')
  await page.getByRole('button', { name: '画面共有を開始' }).click()

  await expect(page.getByText('対象: ブラウザタブ・統合試験用合成タブ')).toBeVisible()
  await expect(page.getByText('認識: 参照可能')).toBeVisible({
    timeout: REAL_RESPONSE_TIMEOUT_MS,
  })

  await page.getByLabel('メッセージ').fill('中央の赤い警告は何？')
  await page.getByRole('checkbox', { name: '現在の画面を参照' }).check()
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('認識: 完了')).toBeVisible({
    timeout: REAL_RESPONSE_TIMEOUT_MS,
  })
  const messages = page.locator('article.message')
  await expect(messages).toHaveCount(2, { timeout: REAL_RESPONSE_TIMEOUT_MS })
  const mioriMessage = messages.nth(1)
  await expect(mioriMessage.locator('.speaker')).toHaveText('光織')
  await expect(mioriMessage.locator('p')).not.toHaveText('')
  await expect(mioriMessage.locator('p')).not.toHaveText('応答の取得に失敗しました。')
})
