import { expect, type Browser } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import {faultToBrowserOffset, type FaultClockCalibration} from './fault-clock'
import {FaultClockRunner, calibrateFaultClock} from './fault-clock-runner'
import type { ControlProbeObservation } from '../src/livekit/control-probe'
import { installScheduledFixture, type ScheduledFixture } from './controlled-audio-fixture'
import { createVoiceChatDriver } from './voice-chat-suite'

declare global {
  interface Window {
    __voiceControlProbeRoom?: {probeControl: () => Promise<ControlProbeObservation>}
  }
}

// 障害なしで実制御往復と旧音声の継続を照合する。再接続100件の合否には使用しない。
export async function measureControlProbeSession(browser: Browser, fixture: ScheduledFixture,
  count: number, output: string): Promise<void> {
  const page = await browser.newPage({baseURL: 'http://localhost:5173', permissions: ['microphone']})
  const driver = createVoiceChatDriver()
  const probes: Array<ControlProbeObservation & {playbackActive: boolean}> = []
  const record: Record<string, unknown> = {measurement_scope: 'livekit_control_probe_session_diagnostic',
    expected_probes: count, fixture_sha256: fixture.audioSha256, probes, outcome: 'failure'}
  let clockRunner: FaultClockRunner | undefined
  let clockBefore: FaultClockCalibration | undefined
  let stage = 'fixture_setup'
  try {
    await installScheduledFixture(page, fixture)
    stage = 'open_voice_chat'
    const microphone = await driver.openVoiceChat(page)
    stage = 'bind_control_probe'
    await page.evaluate(() => {
      const target = window as typeof window & {__digitalSoulsVoiceSessionTestPort?: {
        bindRoom?: (room: NonNullable<Window['__voiceControlProbeRoom']>) => void}}
      if (!target.__digitalSoulsVoiceSessionTestPort) throw new Error('voice diagnostic port unavailable')
      target.__digitalSoulsVoiceSessionTestPort.bindRoom = room => {window.__voiceControlProbeRoom = room}
    })
    if (process.env.VOICE_QUALITY_FAULT_BRIDGE === '1') {
      stage = 'fault_clock_before'
      clockRunner = new FaultClockRunner(resolve(process.cwd(), '..'))
      await clockRunner.ready()
      clockBefore = await calibrateFaultClock(page, clockRunner)
      record.fault_clock_before = clockBefore
    }
    stage = 'session_create'
    const issuedResponse = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname.endsWith('/voice/livekit/token'), {timeout: 10000}).catch(() => null)
    await microphone.click()
    const issued = await issuedResponse
    record.session_create_http_status = issued?.status() ?? null
    if (issued === null || !issued.ok()) throw new Error('session creation unavailable')
    const {session_id: sessionId} = await issued.json() as {session_id?: unknown}
    if (typeof sessionId !== 'string' || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(sessionId)) {
      throw new Error('session identity unavailable')
    }
    record.session_id = sessionId
    stage = 'first_playback'
    await expect(microphone).toHaveAttribute('aria-pressed', 'true')
    await page.evaluate(() => window.__voiceFixtureClock!.start())
    const cycle = await driver.waitForCompletedVoiceCycle(page)
    record.initial_cycle = cycle
    expect(cycle.sessionId).toBe(sessionId)
    stage = 'control_probe'
    for (let index = 0; index < count; index++) {
      probes.push(await page.evaluate(async responseId => {
        const state = window.__voiceChatE2E
        const playbackActive = state.activeResponseId === responseId
          && (state.activeAudioGraphs ?? 0) > 0 && !state.playbackCompletions?.[responseId]
        if (!window.__voiceControlProbeRoom) throw new Error('control probe port unavailable')
        return {...await window.__voiceControlProbeRoom.probeControl(), playbackActive}
      }, cycle.responseId))
      if (index + 1 < count) await page.waitForTimeout(100)
    }
    stage = 'playback_continuity'
    await page.waitForFunction(responseId => !!window.__voiceChatE2E.playbackCompletions?.[responseId], cycle.responseId,
      {timeout: 15000})
    expect(probes.every(probe => probe.status === 'received' && probe.playbackActive)).toBe(true)
    expect(new Set(probes.map(probe => probe.probeId)).size).toBe(count)
    expect(new Set(probes.map(probe => probe.generation)).size).toBe(1)
    const unchanged = await page.evaluate(responseId => {
      const state = window.__voiceChatE2E
      return state.cycles.length === 1 && (state.transportFailures?.length ?? 0) === 0
        && !state.coreEventDiagnostics.some(event => event.type === 'response_cancelled'
          && event.responseId === responseId)
        && state.coreEventDiagnostics.filter(event => event.type === 'response_started').length === 1
    }, cycle.responseId)
    expect(unchanged, 'probe must preserve the original response and transport').toBe(true)
    if (clockRunner && clockBefore) {
      stage = 'fault_clock_after'
      const after = await calibrateFaultClock(page, clockRunner)
      record.fault_clock_after = after
      record.fault_to_browser_offset_ms = faultToBrowserOffset(clockBefore, after)
    }
    record.outcome = 'success'
  } catch {
    record.failure_stage = stage
  } finally {
    record.evidence = await page.evaluate(() => ({
      core_events: window.__voiceChatE2E.coreEventDiagnostics,
      transport_failures: window.__voiceChatE2E.transportFailures ?? [],
      playback_completions: window.__voiceChatE2E.playbackCompletions ?? {},
      packet_playback_observation: window.__voiceChatE2E.lastPacketPlaybackObservation,
      track_media_observation: window.__voiceChatE2E.lastTrackMediaObservation,
      track_response_id: window.__voiceChatE2E.lastTrackMediaResponseId,
      cycles: window.__voiceChatE2E.cycles,
    })).catch(() => ({browser_state_unavailable: true}))
    let ended = false
    if (typeof record.session_id === 'string') {
      const endedResponse = page.waitForResponse(response => response.request().method() === 'DELETE'
        && new URL(response.url()).pathname.endsWith(`/voice/livekit/sessions/${record.session_id}`),
      {timeout: 10000}).catch(() => null)
      try {
        await driver.endVoiceSession(page)
        const response = await endedResponse
        ended = response !== null && response.ok() && (await response.json()).phase === 'ended'
      } catch { await endedResponse }
    } else { await driver.endVoiceSession(page).catch(() => undefined) }
    if (clockRunner) {
      record.fault_clock_process_closed = await clockRunner.close()
      if (!record.fault_clock_process_closed) {record.outcome = 'failure'; record.cleanup_failed = true}
    }
    record.session_end_confirmed = ended
    if (!ended) {record.outcome = 'failure'; record.cleanup_failed = true}
    await mkdir(dirname(output), {recursive: true})
    await writeFile(output, JSON.stringify(record, null, 2) + '\n', {flag: 'wx'})
    await page.evaluate(() => window.__voiceFixtureClock?.close()).catch(() => undefined)
    await page.close()
  }
  expect(record.outcome, 'real control round trips without playback interruption').toBe('success')
}
