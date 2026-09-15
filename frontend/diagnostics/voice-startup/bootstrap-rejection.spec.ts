import { expect, test } from '@playwright/test'

import { hardDeleteSelectedConversation } from '../../playwright/conversation-cleanup'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
} from '../../playwright/resolved-profile'

test.describe.configure({ mode: 'default' })

test.beforeEach(async ({ page }, testInfo) => {
  const profile = await readResolvedProfile()
  await attachProfileEvidence(testInfo, profile)
  const reason = getCapabilitySkipReason(profile, 'voice-chat-real')
  if (reason !== null) test.skip(true, reason)
  // 実APIを透過観測する。取得自体をstub化してfallbackを隠さない。
  await page.addInitScript(() => {
    const target = window as typeof window & { __bootstrapMicrophoneCalls: number }
    target.__bootstrapMicrophoneCalls = 0
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
    navigator.mediaDevices.getUserMedia = (...args) => {
      target.__bootstrapMicrophoneCalls += 1
      return original(...args)
    }
  })
})

test.afterEach(async ({ page }) => {
  await hardDeleteSelectedConversation(page, 'miori')
})

for (const legacy of [
  { core: '1.1', transport: null, detail: 'protocol_version_mismatch' },
  { core: '2.0', transport: null, detail: 'transport_protocol_version_mismatch' },
  { core: '2.0', transport: '1.0', detail: 'transport_protocol_version_mismatch' },
  { core: '2.0', transport: '0.0', detail: 'transport_protocol_version_mismatch' },
]) {
  test(`旧bootstrap Core ${legacy.core} / private ${legacy.transport ?? 'omitted'} を実HTTPで拒否し入力を開始しない`, async ({ page }, testInfo) => {
    let bootstrapRequests = 0
    let vadAssetRequests = 0
    let newWebSockets = 0
    // 旧bundleの実行ではなく、新FEのversion提示だけを旧契約へ変更する。
    // responseは実BEに任せ、409や資源作成結果をmockで補完しない。
    await page.route('**/api/voice/livekit/token', async route => {
      bootstrapRequests += 1
      const body = route.request().postDataJSON() as Record<string, unknown>
      body.protocol_version = legacy.core
      if (legacy.transport === null) delete body.transport_protocol_version
      else body.transport_protocol_version = legacy.transport
      await route.continue({ postData: JSON.stringify(body) })
    })
    page.on('request', request => {
      if (new URL(request.url()).pathname.startsWith('/vad-assets/')) vadAssetRequests += 1
    })
    await page.goto('/')
    await page.getByRole('button', { name: '新規スレッド（光織）' }).click()
    const button = page.getByRole('button', { name: 'マイクをオンにする' })
    await expect(button).toBeEnabled()
    // 初期ページ読込の開発用socketと、操作後の音声接続を区別する。
    page.on('websocket', () => { newWebSockets += 1 })
    const responsePromise = page.waitForResponse(response =>
      response.url().endsWith('/api/voice/livekit/token')
      && response.request().method() === 'POST')
    await button.click()
    const response = await responsePromise
    expect(response.status()).toBe(409)
    expect(await response.json()).toEqual({ detail: {
      code: legacy.detail,
      [legacy.core === '1.1' ? 'supported_protocol_version' : 'supported_transport_protocol_version']: '2.0',
    } })
    await expect(page.getByRole('alert').filter({ hasText: '応答の取得に失敗しました。' })).toBeVisible()
    await expect(page.getByText('セッション: エラー', { exact: true })).toBeVisible()
    await expect(page.getByText('入力: 停止', { exact: true })).toBeVisible()
    await expect(page.getByText('音声会話は停止しました。テキスト履歴は保持されています。', { exact: true })).toBeVisible()
    await expect(button).toHaveAttribute('aria-pressed', 'false')
    await expect(button).not.toHaveClass(/mic-standby|mic-active/)

    // 拒否後1秒の観測窓で自動再試行・暗黙fallbackが始まらないことも確認する。
    // 任意の将来時点や旧bundleそのものの挙動まで証明したとは扱わない。
    await page.waitForTimeout(1_000)
    const microphoneCalls = await page.evaluate(() =>
      (window as typeof window & { __bootstrapMicrophoneCalls: number }).__bootstrapMicrophoneCalls)
    const evidence = {
      source: 'real_http_browser_version_override',
      core_version: legacy.core,
      transport_version: legacy.transport,
      http_status: response.status(),
      rejection_detail: legacy.detail,
      post_rejection_observation_ms: 1_000,
      bootstrap_requests: bootstrapRequests,
      microphone_calls: microphoneCalls,
      vad_asset_requests: vadAssetRequests,
      new_websockets: newWebSockets,
    }
    await testInfo.attach('bootstrap-rejection.real.json', {
      body: JSON.stringify(evidence), contentType: 'application/json',
    })
    expect(bootstrapRequests).toBe(1)
    expect(microphoneCalls).toBe(0)
    expect(vadAssetRequests).toBe(0)
    expect(newWebSockets).toBe(0)
    await expect(button).toHaveAttribute('aria-pressed', 'false')
  })
}

