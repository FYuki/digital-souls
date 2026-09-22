import {expect, test} from '@playwright/test'
import {hardDeleteSelectedConversation} from '../../playwright/conversation-cleanup'
import {attachProfileEvidence, getCapabilitySkipReason, readResolvedProfile} from '../../playwright/resolved-profile'
import {createVoiceChatDriver, createVoiceTestUseOptions} from '../../playwright/voice-chat-suite'

type PermissionProbe = {
  acquired: number
  released: boolean
  tracks: MediaStreamTrack[]
  release?: () => void
}
type ProbedWindow = Window & {__microphonePermissionProbe: PermissionProbe}

test.use(createVoiceTestUseOptions())

test('実ブラウザのtrack取得完了待ち中に会話終了しても遅着trackを停止する', async ({page}, testInfo) => {
  test.setTimeout(90_000)
  const profile = await readResolvedProfile()
  await attachProfileEvidence(testInfo, profile)
  const reason = getCapabilitySkipReason(profile, 'voice-chat-real')
  if (reason !== null) throw new Error('real voice services unavailable')
  // 実ブラウザの合成入力deviceを取得し、Promiseの受渡しだけを保留する。
  // nativeの許可ダイアログや人の実マイクを検証した証拠にはしない。
  await page.addInitScript(() => {
    const state: PermissionProbe = {acquired: 0, released: false, tracks: []}
    ;(window as unknown as ProbedWindow).__microphonePermissionProbe = state
    const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
    navigator.mediaDevices.getUserMedia = async constraints => {
      const stream = await original(constraints)
      state.acquired += 1
      state.tracks.push(...stream.getTracks())
      if (state.acquired === 1) {
        await new Promise<void>(resolve => {state.release = () => {state.released = true; resolve()}})
      }
      return stream
    }
  })
  let deleteCount = 0
  page.on('request', request => {
    if (request.method() === 'DELETE' && /\/api\/voice\/livekit\/sessions\/[^/]+$/.test(new URL(request.url()).pathname)) deleteCount += 1
  })
  try {
    const microphone = await createVoiceChatDriver().openVoiceChat(page)
    await microphone.click()
    await page.waitForFunction(() => {
      const state = (window as unknown as ProbedWindow).__microphonePermissionProbe
      return state.acquired === 1 && state.release !== undefined
    }, undefined, {timeout: 60_000})
    expect(await page.evaluate(() => (window as unknown as ProbedWindow).__microphonePermissionProbe.tracks.map(track => track.readyState))).toEqual(['live'])
    const ended = page.waitForResponse(response => response.request().method() === 'DELETE'
      && /\/api\/voice\/livekit\/sessions\/[^/]+$/.test(new URL(response.url()).pathname))
    await page.getByRole('button', {name: '音声会話を終了', exact: true}).click()
    const response = await ended
    expect(response.status()).toBe(200)
    expect((await response.json() as {phase: string}).phase).toBe('ended')
    await page.evaluate(() => (window as unknown as ProbedWindow).__microphonePermissionProbe.release!())
    await expect.poll(() => page.evaluate(() => (window as unknown as ProbedWindow).__microphonePermissionProbe.tracks.map(track => track.readyState))).toEqual(['ended'])
    await expect(microphone).toHaveAttribute('aria-pressed', 'false')
    await expect(microphone).toBeEnabled()
    await expect(page.getByText('入力: 停止', {exact: true})).toBeVisible()
    const evidence = await page.evaluate(() => ({
      acquired: (window as unknown as ProbedWindow).__microphonePermissionProbe.acquired,
      released: (window as unknown as ProbedWindow).__microphonePermissionProbe.released,
      trackStates: (window as unknown as ProbedWindow).__microphonePermissionProbe.tracks.map(track => track.readyState),
      responseStartedCount: window.__voiceChatE2E.coreEventDiagnostics.filter(event => event.type === 'response_started').length,
    }))
    expect(evidence.responseStartedCount).toBe(0)
    expect(deleteCount).toBe(1)
    await testInfo.attach('microphone-permission-cancel.json', {body: JSON.stringify({
      scope: 'real_browser_synthetic_device_delayed_permission_result', ...evidence,
      deleteCount, endedHttpStatus: response.status(), physicalMicrophoneTested: false,
    }), contentType: 'application/json'})
  } finally {
    await page.evaluate(() => {
      const state = (window as unknown as ProbedWindow).__microphonePermissionProbe
      state?.release?.()
      for (const track of state?.tracks ?? []) track.stop()
    }).catch(() => undefined)
    const end = page.getByRole('button', {name: '音声会話を終了', exact: true})
    if (await end.isVisible()) await end.click()
    await hardDeleteSelectedConversation(page, 'miori')
  }
})
