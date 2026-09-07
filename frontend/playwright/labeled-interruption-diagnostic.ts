import {installServerClockProbe} from './server-clock-probe'
// 固定ラベル音声を実応答の再生中へ入れる。通常応答の100試行とは別の分母を持つ。
import { expect, type Browser, type Page } from '@playwright/test'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { dirname } from 'node:path'
import { installScheduledFixture, parseScheduledFixture, readFixtureBounds, type ScheduledFixture } from './controlled-audio-fixture'
import { createVoiceChatDriver } from './voice-chat-suite'
import type {ShortSpeechEvidence} from '../src/lib/audio/short-speech-evidence'

declare global {
  interface Window {
    __voiceVadDiagnostics?: {frames: {atMs: number; probability: number; rms: number; samples: number; secondary?: ShortSpeechEvidence}[];
      frameOverflow: boolean; eventOverflow: boolean; modelResetOverflow: boolean; modelResets: number[];
      events: {type: string; speechStartedAtMs: number; detectedAtMs: number}[]}
    __digitalSoulsVoiceVadTestPort?: {
      frame: (observation: {atMs: number; probability: number; rms: number; samples: number; secondary?: ShortSpeechEvidence}) => void
      modelReset?: (atMs: number) => void
      event: (event: {type: string; speechStartedAtMs: number; detectedAtMs: number}) => void
    }
  }
}

type Cohort = 'backchannel' | 'take_turn' | 'pause'
type LabeledTrial = {id: string; cohort: string; audio_sha256: string; sample_rate_hz: number; expected_utterances: number; pause_samples: number;
  speech_intervals: {start_sample: number; end_sample: number}[]}
const snapshot = (page: Page) => page.evaluate(() => ({
  vad: window.__voiceVadDiagnostics,
  server_clock: window.__voiceServerClockProbe?.snapshot() ?? null,
  stale_text: window.__voiceStaleTextProbe?.snapshot() ?? null,
  decoded_receipts: window.__voiceChatE2E.decodedReceipts ?? [],
  decoded_receipts_overflow: window.__voiceChatE2E.decodedReceiptsOverflow ?? false,
  stale_audio: window.__voiceChatE2E.staleAudio ?? [],
  stale_audio_overflow: window.__voiceChatE2E.staleAudioOverflow ?? false,
  core_events: window.__voiceChatE2E.coreEventDiagnostics,
  interruptions: window.__voiceChatE2E.interruptions,
  transport_failures: window.__voiceChatE2E.transportFailures ?? [],
  playback_completions: window.__voiceChatE2E.playbackCompletions ?? {},
  network_observations: window.__voiceChatE2E.networkObservations ?? {},
  cycles: window.__voiceChatE2E.cycles,
  fixture_clock_bounds: window.__voiceFixtureClock?.bounds ?? {},
  active_audio_graphs: window.__voiceChatE2E.activeAudioGraphs ?? null,
  active_response_id: window.__voiceChatE2E.activeResponseId ?? null,
  user_control_observation: window.__voiceUserControlProbe?.snapshot(),
}))

export async function measureLabeledInterruptions(browser: Browser, initial: ScheduledFixture,
  cohort: Cohort, count: number, output: string): Promise<void> {
  if (!Number.isInteger(count) || count < 1 || count > 100) throw new Error('invalid interruption trial count')
  const manifestBytes = await readFile(new URL('./fixtures/voice-quality-v2/manifest.json', import.meta.url))
  const manifest = JSON.parse(manifestBytes.toString()) as {trials: LabeledTrial[]}
  const available = manifest.trials.filter(t => t.cohort === cohort)
  const selection = process.env.VOICE_QUALITY_FIXTURE_INDICES
  const indices = selection === undefined ? Array.from({length: count}, (_, i) => i + 1) : selection.split(',').map(Number)
  if (selection !== undefined && (count > 10 || !/^(?:[1-9][0-9]?|100)(?:,(?:[1-9][0-9]?|100))*$/.test(selection)
    || indices.length !== count || new Set(indices).size !== count)) throw new Error('invalid diagnostic fixture selection')
  const selected = indices.map(index => available[index - 1])
  if (selected.some(t => t === undefined)) throw new Error('diagnostic fixture selection unavailable')
  if (selected.length !== count) throw new Error('labeled cohort coverage unavailable')
  const trials: Record<string, unknown>[] = []
  const persist = async () => {
    await mkdir(dirname(output), {recursive: true})
    await writeFile(output, JSON.stringify({measurement_scope: cohort === 'pause' ? 'labeled_livekit_vad_diagnostic' : 'labeled_livekit_interruption_diagnostic',
      measurement_revision: process.env.VOICE_QUALITY_MEASUREMENT_REVISION,
      cohort, expected_measured: count, initial_fixture_sha256: initial.audioSha256,
      ...(selection === undefined ? {} : {fixture_indices: indices}),
      labeled_manifest_sha256: createHash('sha256').update(manifestBytes).digest('hex'), trials}, null, 2) + '\n')
  }
  for (const selectedTrial of selected) {
    if (cohort === 'pause' && (selectedTrial.sample_rate_hz !== 48000
      || selectedTrial.expected_utterances !== 1 || selectedTrial.speech_intervals.length !== 2
      || !Number.isInteger(selectedTrial.pause_samples) || selectedTrial.pause_samples < 1
      || selectedTrial.pause_samples > 28800
      || selectedTrial.speech_intervals[1].start_sample - selectedTrial.speech_intervals[0].end_sample !== selectedTrial.pause_samples)) {
      throw new Error('pause fixture must contain one utterance with a labeled pause of at most 600ms')
    }
    const bytes = await readFile(new URL(`../test-results/vad-quality/fixtures-v2/${selectedTrial.id}.wav`, import.meta.url))
    const interruption = parseScheduledFixture(bytes, {audio_sha256: selectedTrial.audio_sha256,
      sample_rate_hz: selectedTrial.sample_rate_hz,
      speech_start_sample: selectedTrial.speech_intervals[0].start_sample,
      speech_end_sample: selectedTrial.speech_intervals.at(-1)!.end_sample})
    const page = await browser.newPage({baseURL: 'http://localhost:5173', permissions: ['microphone']})
    const driver = createVoiceChatDriver()
    const trial: Record<string, unknown> = {fixture_sha256: selectedTrial.audio_sha256, cohort, outcome: 'failure'}
    let stage = 'initial_response'
    try {
      await page.addInitScript(installServerClockProbe)
      await page.addInitScript(() => {
        const frames: NonNullable<Window['__voiceVadDiagnostics']>['frames'] = []
        const events: NonNullable<Window['__voiceVadDiagnostics']>['events'] = []
        const state = {frames, events, frameOverflow: false, eventOverflow: false,
          modelResetOverflow: false, modelResets: [] as number[]}
        window.__voiceVadDiagnostics = state
        window.__digitalSoulsVoiceVadTestPort = {
          frame: value => {
            if (frames.length >= 1024) {state.frameOverflow = true; return}
            frames.push(value)
          },
          event: value => {
            if (events.length >= 128) {state.eventOverflow = true; return}
            events.push(value)
          },
          modelReset: atMs => {
            if (state.modelResets.length >= 256) {state.modelResetOverflow = true; return}
            state.modelResets.push(atMs)
          },
        }
      })
      await installScheduledFixture(page, initial)
      const microphone = await driver.openVoiceChat(page)
      // 初回の再生が失敗しても、作成済みsessionの終了応答を照合できるよう先に記録する。
      // token本文やsecretは証跡へ保存せず、session_idだけを取り出す。
      const issuedResponse = page.waitForResponse(response => response.request().method() === 'POST'
        && new URL(response.url()).pathname.endsWith('/voice/livekit/token'), {timeout: 10000}).catch(() => null)
      await microphone.click()
      const issued = await issuedResponse
      if (issued === null || !issued.ok()) throw new Error('session creation response unavailable')
      const {session_id: issuedSessionId} = await issued.json() as {session_id?: unknown}
      if (typeof issuedSessionId !== 'string' || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(issuedSessionId)) {
        throw new Error('session creation identity unavailable')
      }
      trial.session_id = issuedSessionId
      stage = 'microphone_activation'
      await expect(microphone).toHaveAttribute('aria-pressed', 'true')
      await page.evaluate(() => window.__voiceUserControlProbe!.begin())
      stage = 'initial_response'
      await page.evaluate(() => window.__voiceFixtureClock!.start())
      const cycle = await driver.waitForCompletedVoiceCycle(page)
      expect(cycle.sessionId, 'created session and initial response correlation').toBe(trial.session_id)
      trial.old_response_id = cycle.responseId
      trial.initial_utterance_id = cycle.utteranceId
      stage = 'playback_overlap'
      await page.waitForFunction(() => window.__voiceFixtureClock?.finished, undefined, {timeout: 5000})
      const playing = await page.evaluate(responseId => {
        const state = window.__voiceChatE2E
        return {observedAtMs: performance.now(), active: state.activeResponseId === responseId
          && (state.activeAudioGraphs ?? 0) > 0 && !state.playbackCompletions?.[responseId],
          initialPlayback: {response_id: responseId,
            track_response_matches: state.lastTrackMediaResponseId === responseId,
            track_media_observation: state.lastTrackMediaObservation,
            packet_playback_observation: state.lastPacketPlaybackObservation}}
      }, cycle.responseId)
      trial.injection_playback = {observedAtMs: playing.observedAtMs, active: playing.active}
      // 割り込み後の別応答に上書きされる前に、旧応答の初回packet証拠を保存する。
      trial.initial_playback = playing.initialPlayback
      expect(playing.active).toBe(true)
      expect(playing.initialPlayback.track_response_matches).toBe(true)
      expect(playing.initialPlayback.packet_playback_observation?.firstOutputAtMs).toBeCloseTo(cycle.startedAt!, 3)
      stage = 'fixture_and_decision'
      await page.evaluate(next => window.__voiceFixtureClock!.replay(next), interruption)
      await page.waitForFunction(() => window.__voiceFixtureClock?.finished, undefined, {timeout: 10000})
      trial.fixture_clock_bounds = await readFixtureBounds(page)
      if (cohort === 'pause') {
        stage = 'vad_completion'
        // fixture全体を出した後に主VADのframe処理まで待つ。先行応答のVADや
        // 文中の最初の終了だけを、対象音声全体の成功と取り違えない。
        await page.waitForFunction(() => {
          const bounds = window.__voiceFixtureClock?.bounds
          const vad = window.__voiceVadDiagnostics
          return bounds?.speechEnd !== undefined && vad !== undefined
            && (vad.frames.at(-1)?.atMs ?? 0) >= bounds.speechEnd.upperMs + 800
        }, undefined, {timeout: 5000})
        const counts = await page.evaluate(() => {
          const start = window.__voiceFixtureClock!.bounds.sourceStart!.lowerMs
          const events = window.__voiceVadDiagnostics!.events.filter(event => event.detectedAtMs >= start)
          return {confirmed: events.filter(event => event.type === 'confirmed').length,
            ended: events.filter(event => event.type === 'ended').length}
        })
        expect(counts).toEqual({confirmed: 1, ended: 1})
        trial.outcome = 'success'
        continue
      }
      await page.waitForFunction(responseId => window.__voiceChatE2E.coreEventDiagnostics.some(event =>
        event.type === 'turn_decision' && event.responseId === responseId && event.final === true), cycle.responseId, {timeout: 10000})
      const decision = await page.evaluate(responseId => window.__voiceChatE2E.coreEventDiagnostics.find(event =>
        event.type === 'turn_decision' && event.responseId === responseId && event.final === true), cycle.responseId)
      trial.decision = decision
      stage = 'cancel_or_continuity'
      if (cohort === 'take_turn') {
        await page.waitForFunction(responseId => window.__voiceChatE2E.coreEventDiagnostics.some(event =>
          event.type === 'response_cancelled' && event.responseId === responseId)
          && window.__voiceChatE2E.interruptions.some(item => item.responseId === responseId), cycle.responseId, {timeout: 5000})
        await page.waitForFunction(responseId => window.__voiceChatE2E.staleAudio?.some(row =>
          row.responseId === responseId && (row.audit.complete || row.audit.missingReason !== null)),
        cycle.responseId, {timeout: 5000})
      } else {
        // ラベルとの一致を検査する前に、保留・誤判定時も旧応答の終端まで観測する。
        await page.waitForFunction(responseId => !!window.__voiceChatE2E.playbackCompletions?.[responseId]
          || window.__voiceChatE2E.coreEventDiagnostics.some(event =>
            event.type === 'response_cancelled' && event.responseId === responseId), cycle.responseId, {timeout: 10000})
        const cancelled = await page.evaluate(responseId => window.__voiceChatE2E.coreEventDiagnostics.some(event =>
          event.type === 'response_cancelled' && event.responseId === responseId), cycle.responseId)
        expect(cancelled).toBe(false)
      }
      expect(decision?.decision).toBe(cohort)
      trial.outcome = 'success'
    } catch {
      trial.failure_stage = stage
      trial.failure_observation = await page.evaluate(() => ({
        micPressed: document.querySelector('button[aria-label^="マイクを"]')?.getAttribute('aria-pressed') === 'true',
        coreEventCount: window.__voiceChatE2E?.coreEventDiagnostics?.length ?? null,
        transportFailures: window.__voiceChatE2E?.transportFailures ?? [],
        clockProbeStatuses: window.__voiceServerClockProbe?.snapshot().observations.map(row => row.status) ?? [],
      })).catch(() => ({browser_state_unavailable: true}))
    } finally {
      trial.evidence = await snapshot(page).catch(() => ({browser_state_unavailable: true}))
      let ended = false
      if (typeof trial.session_id === 'string') {
        const responsePromise = page.waitForResponse(response => response.request().method() === 'DELETE'
          && new URL(response.url()).pathname.endsWith(`/voice/livekit/sessions/${trial.session_id}`), {timeout: 10000})
        try {
          await driver.endVoiceSession(page)
          const response = await responsePromise
          ended = response.ok() && (await response.json()).phase === 'ended'
        } catch { await responsePromise.catch(() => undefined) }
      } else { await driver.endVoiceSession(page).catch(() => undefined) }
      // 終了操作後に届いた出力監視の更新も残す。最初のcomplete行や終了前snapshotだけで判定しない。
      let audioClosed = cohort !== 'take_turn'
      if (cohort === 'take_turn' && typeof trial.old_response_id === 'string') {
        audioClosed = await page.waitForFunction(responseId => {
          const rows = window.__voiceChatE2E.staleAudio?.filter(row => row.responseId === responseId)
          return rows !== undefined && rows.length > 0 && rows.at(-1)!.graphClosed
        }, trial.old_response_id, {timeout: 5000}).then(() => true, () => false)
      }
      trial.cleanup_observation = await page.evaluate(() => ({observedAtMs: performance.now(),
        decoded_receipts: window.__voiceChatE2E.decodedReceipts ?? [],
        decoded_receipts_overflow: window.__voiceChatE2E.decodedReceiptsOverflow ?? false,
        stale_audio: window.__voiceChatE2E.staleAudio ?? [],
        stale_audio_overflow: window.__voiceChatE2E.staleAudioOverflow ?? false,
        stale_text: window.__voiceStaleTextProbe?.close() ?? null,
        server_clock: window.__voiceServerClockProbe?.close() ?? null,
      })).catch(() => ({browser_state_unavailable: true}))
      trial.audio_audit_closed = audioClosed
      trial.session_end_confirmed = ended
      if (!audioClosed) {trial.outcome = 'failure'; trial.audit_cleanup_failed = true}
      if (!ended) {trial.outcome = 'failure'; trial.cleanup_failed = true}
      trials.push(trial)
      await persist()
      await page.evaluate(() => window.__voiceFixtureClock?.close()).catch(() => undefined)
      await page.close()
    }
  }
  expect(trials.filter(trial => trial.outcome === 'success').length, 'successful labeled interruption trial count').toBe(count)
}
