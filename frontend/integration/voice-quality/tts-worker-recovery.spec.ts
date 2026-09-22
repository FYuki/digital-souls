import {execFile} from 'node:child_process'
import {readFileSync} from 'node:fs'
import {fileURLToPath} from 'node:url'
import {promisify} from 'node:util'
import {expect, test} from '@playwright/test'
import {installScheduledFixture, parseScheduledFixture} from '../../playwright/controlled-audio-fixture'
import {createVoiceChatDriver, createVoiceTestUseOptions} from '../../playwright/voice-chat-suite'
import {installFailureOutputProbe, hasConfirmedStoppedOutput} from '../../playwright/tts-failure-output-probe'
import {readResolvedProfile} from '../../playwright/resolved-profile'

const targetFile = process.env.VOICE_TTS_FAULT_TARGET_FILE
const inject = promisify(execFile)
const driver = createVoiceChatDriver()
test.use(createVoiceTestUseOptions())
test.describe.configure({mode: 'serial', retries: 0})
// 各操作のtimeoutは維持し、直列の故障検出・worker回復・次応答を収める。
test.setTimeout(600_000)

for (const phase of ['generation', 'playback'] as const) {
  test('専用Irodoriの実worker停止から同一sessionで復帰する: ' + phase, async ({page}, testInfo) => {
    test.skip(targetFile === undefined, '専用TTS故障対象の明示指定が必要')
    if (!targetFile) throw new Error('dedicated TTS target missing')
    const target = JSON.parse(readFileSync(targetFile, 'utf8'))
    const profile = await readResolvedProfile()
    // runnerは対象のID・image・所有ラベルを別途厳密検査する。
    expect(profile.effectiveProfile).toBe('integration-irodori-cuda-graph')
    const profilePath = process.env.DS_PROFILE_REPORT
    if (!profilePath) throw new Error('resolved profile path missing')
    const rawProfile = JSON.parse(readFileSync(profilePath, 'utf8'))
    expect(rawProfile.dependencies?.irodori?.baseUrl).toBe(target.base_url)
    expect(rawProfile.derivedEnvironment?.IRODORI_BASE_URL).toBe(target.base_url)
    const fixture = parseScheduledFixture(
      readFileSync(new URL('../../playwright/fixtures/speech.wav', import.meta.url)),
      JSON.parse(readFileSync(new URL('../../playwright/fixtures/speech.metadata.json', import.meta.url), 'utf8')),
    )
    await installScheduledFixture(page, fixture)
    await installFailureOutputProbe(page)
    let conversation: string | null = null
    let evidence: Record<string, unknown> = {phase, completed: false}
    try {
      await driver.enableMicrophone(page)
      conversation = await page.evaluate(() => localStorage.getItem('digital-souls:conversation:miori'))
      if (!conversation) throw new Error('owned conversation missing')
      // driverが接続した観測口を連鎖し、Coreの失敗通知時点を出力probeへ伝える。
      await page.evaluate(() => {
        const target = window as typeof window & {
          __digitalSoulsVoiceSessionTestPort?: {receiveCoreEvent?: (event: {type: string}) => void}
        }
        const port = target.__digitalSoulsVoiceSessionTestPort
        if (!port?.receiveCoreEvent) throw new Error('Core event observer missing')
        const original = port.receiveCoreEvent
        port.receiveCoreEvent = event => {
          if (event.type === 'response_failed') window.__ttsFailureOutput!.markFailure()
          original(event)
        }
      })
      const input = page.getByLabel('メッセージ')
      await input.fill('星空の楽しみについて、一文ずつ区切って、二十文で詳しく説明してください。')
      await input.press('Enter')
      const handle = await page.waitForFunction(() =>
        window.__voiceChatE2E.coreEventDiagnostics.find(event => event.type === 'response_started')?.responseId,
      undefined, {timeout: 60_000})
      const old = await handle.jsonValue()
      if (typeof old !== 'string') throw new Error('initial response missing')
      await expect(input).toHaveValue('', {timeout: 60_000})
      if (phase === 'playback') {
        await page.waitForFunction(id => window.__voiceChatE2E.liveKitOrder.includes(id + ':rendered-audio'),
          old, {timeout: 60_000})
      }
      const before = await page.evaluate(id => ({
        rendered: window.__voiceChatE2E.liveKitOrder.includes(id + ':rendered-audio'),
        terminal: window.__voiceChatE2E.coreEventDiagnostics.some(event => event.responseId === id
          && ['response_completed', 'response_cancelled', 'response_failed'].includes(String(event.type))),
      }), old)
      evidence = {...evidence, before}
      expect(before).toEqual({rendered: phase === 'playback', terminal: false})
      const fault = await inject('python3', [
        fileURLToPath(new URL('../../../scripts/voice_quality/tts_worker_fault.py', import.meta.url)),
        '--target-file', targetFile,
      ], {timeout: 60_000, maxBuffer: 64 * 1024})
      const faultResult = JSON.parse(fault.stdout)
      evidence = {...evidence, fault: faultResult}
      expect(faultResult.active_observed).toBe(1)
      await page.waitForFunction(id => window.__voiceChatE2E.coreEventDiagnostics.some(event =>
        event.type === 'response_failed' && event.responseId === id), old, {timeout: 60_000})
      await expect(page.locator('article.message.failed[data-failed-voice-turn="' + old + '"]')).toBeVisible()
      await expect(page.getByRole('button', {name: '音声会話を終了'})).toBeVisible()
      if (phase === 'playback') {
        await expect.poll(async () => hasConfirmedStoppedOutput(
          await page.evaluate(() => window.__ttsFailureOutput!.snapshot()),
        ), {timeout: 10_000}).toBe(true)
      } else {
        expect(await page.evaluate(id => window.__voiceChatE2E.liveKitOrder.includes(id + ':rendered-audio'), old)).toBe(false)
      }
      const stoppedOutput = await page.evaluate(() => window.__ttsFailureOutput!.snapshot())
      await expect.poll(async () => {
        try {
          const response = await page.request.get(target.base_url + '/health/ready', {timeout: 2000})
          return response.status() === 200
        } catch {return false}
      }, {timeout: 120_000}).toBe(true)
      // 固定音声を実STTへ送り、textだけの回復で代用しない。
      await input.blur()
      await page.evaluate(async () => {await window.__voiceFixtureClock!.start()})
      const completed = await page.waitForFunction(previous => {
        const next = window.__voiceChatE2E.coreEventDiagnostics.find(event =>
          event.type === 'response_started' && event.responseId !== previous)?.responseId
        if (typeof next !== 'string') return null
        const playback = window.__voiceChatE2E.playbackCompletions?.[next]
        const terminal = window.__voiceChatE2E.coreEventDiagnostics.find(event =>
          event.responseId === next && event.type === 'response_completed')
        return playback && terminal ? {next, playback} : null
      }, old, {timeout: 90_000})
      const result = await completed.jsonValue()
      if (!result) throw new Error('recovered playback missing')
      expect(result.playback.renderedSamples).toBeGreaterThan(0)
      expect(result.playback.renderedSamples).toBe(result.playback.expectedSamples)
      expect(result.playback.gapSamples).toBe(0)
      const final = await page.evaluate(previous => ({
        terminals: window.__voiceChatE2E.coreEventDiagnostics.filter(event => event.responseId === previous
          && ['response_failed', 'response_completed', 'response_cancelled'].includes(String(event.type))).map(event => event.type),
        sessions: [...new Set(window.__voiceChatE2E.coreEventDiagnostics.map(event => event.sessionId).filter(Boolean))],
        oldCompleted: window.__voiceChatE2E.playbackCompletions?.[previous] !== undefined,
        transportFailures: window.__voiceChatE2E.transportFailures ?? [],
        output: window.__ttsFailureOutput!.snapshot(),
        probeMissing: window.__ttsFailureOutput!.missing(),
      }), old)
      expect(final.probeMissing).toBeNull()
      expect(final.terminals).toEqual(['response_failed'])
      expect(final.sessions).toHaveLength(1)
      expect(final.oldCompleted).toBe(false)
      expect(final.transportFailures).toEqual([])
      // 次応答のnodeを含めず、失敗応答に属したnodeだけを最後まで追跡する。
      expect(final.output.slice(0, stoppedOutput.length).every(row => row.nonzeroAfter === 0)).toBe(true)
      evidence = {phase, completed: true, before, fault: faultResult, stoppedOutput, recovered: result, final}
    } finally {
      const lastObservation = await page.evaluate(() => ({
        coreEvents: window.__voiceChatE2E?.coreEventDiagnostics ?? [],
        output: window.__ttsFailureOutput?.snapshot() ?? [],
        probeMissing: window.__ttsFailureOutput?.missing() ?? null,
        transportFailures: window.__voiceChatE2E?.transportFailures ?? [],
        microphoneStates: window.__voiceChatE2E?.micStates ?? [],
        mediaTimelineInterruptions: window.__voiceChatE2E?.mediaTimelineInterruptions ?? [],
        mediaPacketLosses: window.__voiceChatE2E?.mediaPacketLosses ?? [],
      })).catch(() => null)
      evidence = {...evidence, lastObservation}
      await testInfo.attach('tts-worker-recovery.json', {body: JSON.stringify(evidence), contentType: 'application/json'})
      try {await driver.endVoiceSession(page)} finally {
        try {await page.evaluate(() => window.__voiceFixtureClock?.close())} finally {
          if (conversation) {
            const deleted = await page.request.delete('/api/characters/miori/conversations/' + conversation)
            expect(deleted.status()).toBe(204)
          }
        }
      }
    }
  })
}
