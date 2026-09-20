import {afterEach, expect, test, vi} from 'vitest'
import type {Locator, Page, Response, TestInfo} from '@playwright/test'
import {isIssuedSessionResponse, startMeasuredSession} from '../playwright/start-measured-session'

afterEach(() => vi.restoreAllMocks())
const sessionId = '12345678-1234-1234-1234-123456789abc'
const response = (status = 200, body: unknown = {session_id: sessionId, reconnect_grace_ms: 10000, token: 'PRIVATE_SENTINEL'}) =>
  ({status: () => status, request: () => ({method: () => 'POST'}),
    url: () => 'http://localhost/api/voice/livekit/token', json: async () => body}) as unknown as Response

function harness() {
  let completeResponse!: (value: Response) => void
  let rejectResponse!: (error: Error) => void
  let predicate!: (value: Response) => boolean
  let completeReady!: (value: {jsonValue: () => Promise<string>}) => void
  const waitForResponse = vi.fn((filter: (value: Response) => boolean) => {
    predicate = filter
    return new Promise<Response>((resolve, reject) => {completeResponse = resolve; rejectResponse = reject})
  })
  const waitForFunction = vi.fn(() => new Promise(resolve => {completeReady = resolve}))
  const page = {waitForResponse, waitForFunction} as unknown as Page
  const setTimeout = vi.fn()
  const info = {timeout: 120000, setTimeout} as unknown as TestInfo
  const click = vi.fn(async () => {})
  const observed = vi.fn()
  const result = startMeasuredSession(page, {click} as unknown as Locator, info, observed)
  const settled = vi.fn()
  const outcome = result.then(() => settled(), error => {settled(); return error})
  return {waitForResponse, setTimeout, observed, settled, outcome,
    emit: (r: Response) => {if (predicate(r)) completeResponse(r)},
    close: () => rejectResponse(new Error('PRIVATE_SENTINEL')),
    ready: (value: string) => completeReady({jsonValue: async () => value})}
}

test('202を成功扱いにせず、固定期限なしで200と発話受付の両方を待つ', async () => {
  const clock = vi.spyOn(performance, 'now').mockReturnValue(100)
  const h = harness()
  await vi.waitFor(() => expect(h.setTimeout).toHaveBeenCalledWith(0))
  expect(h.waitForResponse).toHaveBeenCalledWith(expect.any(Function), {timeout: 0})
  h.emit(response(202, {phase: 'preparing'}))
  await Promise.resolve()
  expect(h.observed).not.toHaveBeenCalled()
  clock.mockReturnValue(200100)
  expect(h.settled).not.toHaveBeenCalled()
  h.emit(response())
  await vi.waitFor(() => expect(h.observed).toHaveBeenCalledWith({sessionId, reconnectGraceMs: 10000, httpStatus: 200}))
  expect(JSON.stringify(h.observed.mock.calls)).not.toContain('PRIVATE_SENTINEL')
  expect(h.settled).not.toHaveBeenCalled()
  h.ready('ready')
  expect(await h.outcome).toBeUndefined()
  expect(h.setTimeout).toHaveBeenLastCalledWith(320000)
})

test.each([false, true])('準備エラーを200待ちで隠さず、発行済み=%sなら終了確認用IDを残す', async issued => {
  const h = harness()
  await vi.waitFor(() => expect(h.setTimeout).toHaveBeenCalledWith(0))
  if (issued) {
    h.emit(response())
    await vi.waitFor(() => expect(h.observed).toHaveBeenCalledTimes(1))
  }
  h.ready('error')
  expect((await h.outcome)?.message).toBe('voice_preparation_failed')
  if (!issued) {expect(h.observed).not.toHaveBeenCalled(); h.close()}
})

test('応答本文の不正値・認証情報をエラーへ転記しない', async () => {
  const h = harness()
  await vi.waitFor(() => expect(h.setTimeout).toHaveBeenCalledWith(0))
  h.emit(response(200, {session_id: 'PRIVATE_SENTINEL', reconnect_grace_ms: 10000}))
  h.ready('ready')
  expect((await h.outcome)?.message).toBe('session_creation_identity_unavailable')
  expect(h.observed).not.toHaveBeenCalled()
})

test('別経路・失敗応答はSession発行の証拠にしない', () => {
  expect(isIssuedSessionResponse(response(202))).toBe(false)
  expect(isIssuedSessionResponse(response(503))).toBe(false)
  expect(isIssuedSessionResponse({...response(), url: () => 'http://localhost/other'} as Response)).toBe(false)
  expect(isIssuedSessionResponse({...response(), request: () => ({method: () => 'GET'})} as Response)).toBe(false)
})
