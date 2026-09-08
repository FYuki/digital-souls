// 発話を供給せず、実Roomの正常終了と切断後の終了を確認する。通常100件とは別の診断。
import { expect, type Browser } from '@playwright/test'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { installScheduledFixture, type ScheduledFixture } from './controlled-audio-fixture'
import { createVoiceChatDriver } from './voice-chat-suite'

type NativeRow = {
  session_id: string
  name: string
  reason?: string | null
  response_id?: string | null
  summary?: { microphone_activation_attempts: number; end_requested: boolean } | null
}

export async function measureZeroResponseSessions(
  browser: Browser, fixture: ScheduledFixture, output: string,
): Promise<void> {
  const dataRoot = process.env.DS_DATA_DIR
  if (!dataRoot) throw new Error('dedicated test data root is required')
  const journal = join(dataRoot, 'voice-metrics', 'sessions', 'controlled-trace.jsonl')
  const cases: Record<string, unknown>[] = []
  const persist = async () => {
    await mkdir(dirname(output), { recursive: true })
    await writeFile(output, JSON.stringify({
      measurement_scope: 'native_zero_response_session_lifecycle_diagnostic',
      measurement_revision: process.env.VOICE_QUALITY_MEASUREMENT_REVISION,
      expected_sessions: 2, supplied_utterances: 0, cases,
    }, null, 2) + '\n')
  }
  const readSession = async (sessionId: string): Promise<NativeRow[]> => {
    try {
      return (await readFile(journal, 'utf8')).trim().split('\n').filter(Boolean)
        .map(line => JSON.parse(line) as NativeRow).filter(row => row.session_id === sessionId)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return []
      throw error
    }
  }
  for (const kind of ['explicit_end', 'browser_disconnect'] as const) {
    const page = await browser.newPage({ baseURL: 'http://localhost:5173', permissions: ['microphone'] })
    const driver = createVoiceChatDriver()
    const record: Record<string, unknown> = { kind, outcome: 'failure' }
    cases.push(record)
    let stage = 'session_create'
    let ended = false
    let sessionId: string | undefined
    let deleteRequests = 0
    page.on('request', request => {
      if (request.method() === 'DELETE' && sessionId !== undefined
        && new URL(request.url()).pathname.endsWith(`/voice/livekit/sessions/${sessionId}`)) deleteRequests++
    })
    try {
      // start/replayを呼ばないため、マイクtrackには無音だけを流す。
      await installScheduledFixture(page, fixture)
      const microphone = await driver.openVoiceChat(page)
      const issuedResponse = page.waitForResponse(response => response.request().method() === 'POST'
        && new URL(response.url()).pathname.endsWith('/voice/livekit/token'), { timeout: 10_000 })
      await microphone.click()
      const issued = await issuedResponse
      expect(issued.ok()).toBe(true)
      const { session_id: issuedId, reconnect_grace_ms: graceMs } = await issued.json() as {
        session_id: unknown; reconnect_grace_ms: unknown
      }
      // 認証tokenと接続先はmanifestへ保存しない。
      if (typeof issuedId !== 'string' || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(issuedId)
        || typeof graceMs !== 'number' || !Number.isInteger(graceMs) || graceMs < 1 || graceMs > 120_000) {
        throw new Error('session identity or reconnect deadline unavailable')
      }
      sessionId = issuedId
      record.session_id = sessionId
      record.reconnect_grace_ms = graceMs
      await expect(microphone).toHaveAttribute('aria-pressed', 'true')
      stage = 'native_activation'
      await expect.poll(async () => {
        const rows = await readSession(issuedId)
        return rows.some(row => row.name === 'activated') && rows.some(row => row.name === 'summary'
          && row.summary?.microphone_activation_attempts === 1 && row.summary.end_requested === false)
      }, { timeout: 10_000 }).toBe(true)
      const before = await page.evaluate(() => ({
        completed_cycles: window.__voiceChatE2E.cycles.length,
        started_responses: window.__voiceChatE2E.coreEventDiagnostics.filter(row => row.type === 'response_started').length,
        playback_completions: Object.keys(window.__voiceChatE2E.playbackCompletions ?? {}).length,
        fixture_finished: window.__voiceFixtureClock?.finished ?? false,
      }))
      expect(before.completed_cycles).toBe(0)
      expect(before.started_responses).toBe(0)
      expect(before.playback_completions).toBe(0)
      expect(before.fixture_finished).toBe(false)
      record.before_end = before
      stage = kind
      const endObservedFromMs = performance.now()
      if (kind === 'explicit_end') {
        const response = page.waitForResponse(value => value.request().method() === 'DELETE'
          && new URL(value.url()).pathname.endsWith(`/voice/livekit/sessions/${issuedId}`))
        await driver.endVoiceSession(page)
        const result = await response
        expect(result.ok()).toBe(true)
        expect((await result.json()).phase).toBe('ended')
        expect(deleteRequests).toBe(1)
      } else {
        // この診断が所有するpageだけを閉じ、明示終了APIを呼ばない。
        await page.close({ runBeforeUnload: false })
        expect(deleteRequests).toBe(0)
      }
      stage = 'native_end'
      const expectedReason = kind === 'explicit_end' ? 'explicit' : 'reconnect_timeout'
      await expect.poll(async () => (await readSession(issuedId)).find(row => row.name === 'ended')?.reason,
        // SFUの切断検知はpage.closeより遅れる。再接続猶予はserver側の通知から起算する。
        { timeout: kind === 'explicit_end' ? 10_000 : graceMs + 60_000, intervals: [100, 250, 500] }).toBe(expectedReason)
      const rows = await readSession(issuedId)
      expect(rows.filter(row => row.name === 'created')).toHaveLength(1)
      expect(rows.filter(row => row.name === 'activated')).toHaveLength(1)
      expect(rows.filter(row => row.name === 'ended')).toHaveLength(1)
      expect(rows.some(row => row.response_id != null)).toBe(false)
      expect(rows.some(row => ['invalid_summary', 'cleanup_failed', 'overflow'].includes(row.name))).toBe(false)
      expect(rows.some(row => row.summary?.end_requested === true)).toBe(kind === 'explicit_end')
      ended = true
      Object.assign(record, { outcome: 'success', native_end_reason: expectedReason,
        explicit_delete_requests: deleteRequests, native_completed_responses: 0,
        native_end_confirmed: true, end_observation_elapsed_ms: performance.now() - endObservedFromMs,
        final_operation_window_observed: kind === 'explicit_end' })
    } catch (error) {
      record.failure_stage = stage
      throw error
    } finally {
      await persist()
      if (!page.isClosed()) {
        if (!ended) await driver.endVoiceSession(page).catch(() => undefined)
        await page.evaluate(() => window.__voiceFixtureClock?.close()).catch(() => undefined)
        await page.close()
      }
    }
  }
}
