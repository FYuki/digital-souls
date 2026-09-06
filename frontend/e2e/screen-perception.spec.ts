import { expect, test, type Page, type TestInfo } from '@playwright/test'

import { installMockUiBootstrap } from './mock-ui-bootstrap'
import {
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
  type ResolvedProfile,
} from '../playwright/resolved-profile'

const CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const CLIENT_SESSION_ID = '20000000-0000-4000-8000-000000000001'
const SCREEN_SESSION_ID = '40000000-0000-4000-8000-000000000001'
const ROUTING_REVISION = 'a'.repeat(64)

type ScreenEvidence = {
  sessionStarts: Array<{ requestedSurface: string; actualSurface: string }>
  uploads: Array<{ surface: string | null; byteLength: number }>
  revocations: string[]
  failures: string[]
  contextualReferenceResponseMs?: number
}

let resolvedProfile: ResolvedProfile

test.describe.configure({ mode: 'serial' })

test.beforeAll(async () => {
  resolvedProfile = await readResolvedProfile()
})

test.beforeEach(async ({ page }, testInfo) => {
  await attachProfileEvidence(testInfo, resolvedProfile)
  const reason = getCapabilitySkipReason(resolvedProfile, 'mocked-e2e')
  if (reason !== null) test.skip(true, reason)
  await installSyntheticScreenCapture(page)
})

const installSyntheticScreenCapture = async (page: Page) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'userActivation', {
      configurable: true,
      value: { isActive: true },
    })
    Object.defineProperty(navigator.mediaDevices, 'getDisplayMedia', {
      configurable: true,
      value: async (options: DisplayMediaStreamOptions) => {
        const canvas = document.createElement('canvas')
        canvas.width = 640
        canvas.height = 360
        const stream = canvas.captureStream(10)
        const track = stream.getVideoTracks()[0]
        if (track === undefined) throw new Error('synthetic video track is required')
        const requested = (options.video as MediaTrackConstraints | undefined)
          ?.displaySurface as string | undefined
        Object.defineProperty(track, 'getSettings', {
          configurable: true,
          value: () => ({ displaySurface: requested ?? 'monitor' }),
        })
        Object.defineProperty(track, 'label', {
          configurable: true,
          value: '合成画面',
        })
        return stream
      },
    })
    Object.defineProperties(HTMLVideoElement.prototype, {
      videoWidth: { configurable: true, get: () => 640 },
      videoHeight: { configurable: true, get: () => 360 },
      readyState: {
        configurable: true,
        get: () => HTMLMediaElement.HAVE_CURRENT_DATA,
      },
      requestVideoFrameCallback: {
        configurable: true,
        value: (callback: () => void) => {
          if ((window as unknown as { __digitalSoulsStaticScreen?: boolean }).__digitalSoulsStaticScreen) return 1
          queueMicrotask(callback)
          return 1
        },
      },
      cancelVideoFrameCallback: { configurable: true, value: () => undefined },
    })
    Object.defineProperty(HTMLMediaElement.prototype, 'play', {
      configurable: true,
      value: async () => undefined,
    })
    Object.defineProperty(HTMLMediaElement.prototype, 'pause', {
      configurable: true,
      value: () => undefined,
    })
    Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
      configurable: true,
      value: (type: string) => type === '2d' ? { drawImage: () => undefined } : null,
    })
    Object.defineProperty(HTMLCanvasElement.prototype, 'toBlob', {
      configurable: true,
      value: (callback: BlobCallback, type?: string) => {
        callback(new Blob([new Uint8Array(32)], { type: type ?? 'image/png' }))
      },
    })
  })
}

const eventBase = (type: string) => ({
  protocol_version: '1.0',
  type,
  event_id: crypto.randomUUID(),
})

const installScreenBackend = async (page: Page): Promise<ScreenEvidence> => {
  const evidence: ScreenEvidence = {
    sessionStarts: [],
    uploads: [],
    revocations: [],
    failures: [],
  }
  let conversationCreated = false
  let requestIndex = 0
  let activeGeneration = 1

  await page.route('**/api/characters/**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    if (pathname.endsWith('/turns')) {
      await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
      return
    }
    if (request.method() === 'POST') conversationCreated = true
    const conversation = {
      character_id: 'miori',
      conversation_id: CONVERSATION_ID,
      created_at: '2026-09-06T03:00:00Z',
      updated_at: '2026-09-06T03:00:00Z',
      archived_at: null,
      title: CONVERSATION_ID,
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(request.method() === 'POST'
        ? conversation
        : conversationCreated ? [conversation] : []),
    })
  })
  await installMockUiBootstrap(page)

  await page.route('**/api/perception/screen/routing', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...eventBase('screen_routing_disclosed'),
        client_session_id: CLIENT_SESSION_ID,
        routing_revision: ROUTING_REVISION,
        vision_destination: 'local',
        chat_destination: 'local',
        limits: {
          allowed_mime_types: ['image/png', 'image/jpeg'],
          capture_timeout_ms: 5_000,
          max_bytes: 5_242_880,
          max_concurrency: 1,
          max_height: 2560,
          max_pixels: 4_194_304,
          max_width: 2560,
          request_timeout_ms: 45_000,
          snapshot_max_age_ms: 5_000,
          vision_timeout_ms: 30_000,
        },
      }),
    })
  })
  await page.route('**/api/perception/screen/sessions', async (route) => {
    const body = route.request().postDataJSON() as Record<string, string>
    activeGeneration = Number(body.generation)
    evidence.sessionStarts.push({
      requestedSurface: body.requested_surface,
      actualSurface: body.actual_surface,
    })
    await route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({
        ...eventBase('screen_session_started'),
        screen_session_id: SCREEN_SESSION_ID,
        client_session_id: CLIENT_SESSION_ID,
        generation: body.generation,
        character_id: 'miori',
        conversation_id: CONVERSATION_ID,
        actual_surface: body.actual_surface,
        routing_revision: ROUTING_REVISION,
        heartbeat_interval_ms: 5_000,
        lease_duration_ms: 15_000,
        lease_expires_at: '2099-01-01T00:00:15Z',
      }),
    })
  })
  await page.route('**/api/perception/screen/sessions/**', async (route) => {
    const body = route.request().postDataJSON() as Record<string, string>
    if (route.request().method() === 'DELETE') {
      evidence.revocations.push(body.reason)
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...eventBase('screen_session_revoked'),
          screen_session_id: SCREEN_SESSION_ID,
          generation: body.generation,
          reason: body.reason,
        }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...eventBase('screen_session_heartbeat_accepted'),
        screen_session_id: SCREEN_SESSION_ID,
        generation: body.generation,
        lease_expires_at: '2099-01-01T00:00:15Z',
      }),
    })
  })
  await page.route('**/api/chat', async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>
    const message = String(body.message)
    const inspect = body.screen_reference === true || message === 'これ何？'
    if (inspect) {
      requestIndex += 1
      await route.fulfill({
        status: 202,
        contentType: 'application/json',
        body: JSON.stringify({
          ...eventBase('screen_snapshot_requested'),
          screen_session_id: SCREEN_SESSION_ID,
          generation: activeGeneration,
          request_id: `50000000-0000-4000-8000-${String(requestIndex).padStart(12, '0')}`,
          turn_id: `60000000-0000-4000-8000-${String(requestIndex).padStart(12, '0')}`,
          source: body.screen_reference === true ? 'explicit_ui' : 'natural_language_text',
          requested_at: new Date().toISOString(),
          capture_deadline: new Date(Date.now() + 5_000).toISOString(),
        }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(chatTurn(message, `画面を使わずに回答${message}`)),
    })
  })
  await page.route('**/api/perception/screen/requests/*/image', async (route) => {
    const headers = route.request().headers()
    evidence.uploads.push({
      surface: headers['x-screen-surface'] ?? null,
      byteLength: route.request().postDataBuffer()?.byteLength ?? 0,
    })
    const requestId = headers['x-screen-request-id']
      ?? new URL(route.request().url()).pathname.split('/').at(-2)
      ?? ''
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        upload: {
          ...eventBase('screen_snapshot_upload_accepted'),
          screen_session_id: SCREEN_SESSION_ID,
          generation: Number(headers['x-screen-generation']),
          request_id: requestId,
          image_id: headers['x-screen-image-id'],
          received_at: new Date().toISOString(),
        },
        ...chatTurn('これ何？', `画面を一度だけ確認して回答${evidence.uploads.length}`),
      }),
    })
  })
  await page.route('**/api/perception/screen/requests/*/failure', async (route) => {
    const failure = route.request().postDataJSON() as Record<string, unknown>
    evidence.failures.push(String(failure.reason_code))
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        error: failure,
        ...chatTurn('これ何？', `画面を確認できませんでした:${String(failure.reason_code)}`),
      }),
    })
  })
  return evidence
}

const chatTurn = (userContent: string, assistantContent: string) => ({
  character: 'miori',
  turn: {
    kind: 'content',
    turn_id: crypto.randomUUID(),
    user_content: userContent,
    assistant_content: assistantContent,
  },
})

const openScreenChat = async (page: Page): Promise<ScreenEvidence> => {
  const evidence = await installScreenBackend(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新規スレッド（光織）' }).click()
  return evidence
}

const attachMetadataEvidence = async (testInfo: TestInfo, evidence: ScreenEvidence) => {
  await testInfo.attach('screen-perception.mock.json', {
    body: JSON.stringify(evidence, null, 2),
    contentType: 'application/json',
  })
}

test('共有ONだけでは送信せず文脈参照と明示参照で各1枚だけ送る', async ({ page }, testInfo) => {
  const evidence = await openScreenChat(page)

  await page.getByText('画面共有の詳細').click()
  await page.getByRole('button', { name: '画面共有を開始' }).click()
  await expect(page.getByText('共有: 共有中')).toBeVisible()
  await expect(page.getByText('認識: 参照可能')).toBeVisible()
  expect(evidence.uploads).toHaveLength(0)

  await page.getByLabel('メッセージ').fill('今日は元気？')
  await page.getByRole('button', { name: '送信' }).click()
  await expect(page.getByText('画面を使わずに回答今日は元気？')).toBeVisible()
  expect(evidence.uploads).toHaveLength(0)

  const contextualReferenceStartedAt = Date.now()
  await page.getByLabel('メッセージ').fill('これ何？')
  await page.getByRole('button', { name: '送信' }).click()
  await expect.poll(() => evidence.uploads.length + evidence.failures.length).toBe(1)
  expect(evidence.failures).toEqual([])
  await expect(page.getByText('画面を一度だけ確認して回答1')).toBeVisible()
  expect(evidence.uploads).toEqual([{ surface: 'monitor', byteLength: expect.any(Number) }])
  expect(evidence.uploads[0]?.byteLength).toBeGreaterThan(0)

  await page.getByLabel('メッセージ').fill('明示参照')
  await page.getByRole('checkbox', { name: '現在の画面を参照' }).check()
  await page.getByRole('button', { name: '送信' }).click()
  await expect(page.getByText('画面を一度だけ確認して回答2')).toBeVisible()
  expect(evidence.uploads).toHaveLength(2)

  await attachMetadataEvidence(testInfo, {
    ...evidence,
    contextualReferenceResponseMs: Date.now() - contextualReferenceStartedAt,
  })
})

test('ウィンドウ共有はsurfaceを維持しOFFと再読込でsessionを復元しない', async ({ page }, testInfo) => {
  const evidence = await openScreenChat(page)
  await page.getByText('画面共有の詳細').click()
  await page.getByLabel('共有する画面の種類').selectOption('window')
  await page.getByRole('button', { name: '画面共有を開始' }).click()
  await expect(page.getByText('認識: 参照可能')).toBeVisible()

  await page.getByLabel('メッセージ').fill('これ何？')
  await page.getByRole('button', { name: '送信' }).click()
  await expect(page.getByText('画面を一度だけ確認して回答1')).toBeVisible()
  expect(evidence.uploads[0]?.surface).toBe('window')

  await page.getByRole('button', { name: '画面共有を停止' }).click()
  await expect(page.getByText('共有: 停止中')).toBeVisible()
  await expect.poll(() => evidence.revocations).toContain('user_off')
  await expect(page.getByRole('checkbox', { name: '現在の画面を参照' })).toBeDisabled()

  await page.reload()
  await expect(page.getByText('対象: 未選択')).toBeVisible()
  expect(evidence.sessionStarts).toHaveLength(1)
  expect(evidence.failures).toEqual([])
  await attachMetadataEvidence(testInfo, evidence)
})

test('Edgeの静止ウィンドウ相当でも5秒の取得期限内に現在frameを送る', async ({ page }, testInfo) => {
  const evidence = await openScreenChat(page)
  await page.getByText('画面共有の詳細').click()
  await page.getByLabel('共有する画面の種類').selectOption('window')
  await page.getByRole('button', { name: '画面共有を開始' }).click()
  await expect(page.getByText('対象: ウィンドウ・合成画面')).toBeVisible()
  await page.evaluate(() => {
    (window as unknown as { __digitalSoulsStaticScreen?: boolean }).__digitalSoulsStaticScreen = true
  })

  await page.getByLabel('メッセージ').fill('これ何？')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('画面を一度だけ確認して回答1')).toBeVisible()
  expect(evidence.uploads).toEqual([{ surface: 'window', byteLength: expect.any(Number) }])
  expect(evidence.failures).toEqual([])
  await attachMetadataEvidence(testInfo, evidence)
})

test('ブラウザタブ共有はbrowser surfaceを維持して送信する', async ({ page }, testInfo) => {
  const evidence = await openScreenChat(page)
  await page.getByText('画面共有の詳細').click()
  await page.getByLabel('共有する画面の種類').selectOption('browser')
  await page.getByRole('button', { name: '画面共有を開始' }).click()
  await expect(page.getByText('対象: ブラウザタブ・合成画面')).toBeVisible()

  await page.getByRole('button', { name: 'サイドバーを閉じる' }).click()
  await expect(page.getByRole('button', { name: 'サイドバーを開く' })).toBeVisible()
  expect(evidence.revocations).toEqual([])
  await page.getByRole('button', { name: 'サイドバーを開く' }).click()
  await expect(page.getByText('対象: ブラウザタブ・合成画面')).toBeVisible()
  expect(evidence.sessionStarts).toHaveLength(1)

  await page.getByLabel('メッセージ').fill('これ何？')
  await page.getByRole('button', { name: '送信' }).click()

  await expect(page.getByText('画面を一度だけ確認して回答1')).toBeVisible()
  expect(evidence.uploads[0]?.surface).toBe('browser')
  expect(evidence.failures).toEqual([])
  await attachMetadataEvidence(testInfo, evidence)
})
