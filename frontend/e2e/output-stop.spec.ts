import {test, expect} from '@playwright/test'
import {build} from 'esbuild'
import {fileURLToPath} from 'node:url'
import {attachProfileEvidence, getCapabilitySkipReason, readResolvedProfile} from '../playwright/resolved-profile'
import type {checkBrowserOutputStop} from '../playwright/output-stop-check'

declare global {
  interface Window {OutputStopCheck: {checkBrowserOutputStop: typeof checkBrowserOutputStop}}
}
let script: string

test.beforeAll(async () => {
  const result = await build({entryPoints: [fileURLToPath(new URL('../playwright/output-stop-check.ts', import.meta.url))],
    bundle: true, write: false, format: 'iife', globalName: 'OutputStopCheck'})
  script = result.outputFiles[0].text
})
for (let trial = 1; trial <= 3; trial++) {
  test(`実ブラウザ出力時計で停止を確認する ${trial}`, async ({page}, testInfo) => {
    const resolvedProfile = await readResolvedProfile()
    await attachProfileEvidence(testInfo, resolvedProfile)
    const reason = getCapabilitySkipReason(resolvedProfile, 'mocked-e2e')
    test.skip(reason !== null, reason ?? '')
    // 合成ページだけを置換する。WebAudio・worklet・出力時計はブラウザの実装をそのまま使う。
    await page.route('**/output-stop-check', route => route.fulfill({contentType: 'text/html; charset=utf-8', body: '<meta charset="utf-8"><button>音声の停止を検証</button>'}))
    await page.goto('/output-stop-check')
    await page.addScriptTag({content: script})
    await page.getByRole('button', {name: '音声の停止を検証'}).click()
    const result = await page.evaluate(() => window.OutputStopCheck.checkBrowserOutputStop())
    await testInfo.attach('output-stop-result', {body: JSON.stringify(result), contentType: 'application/json'})
    expect(result).toMatchObject({outputClockPassedStop: true, graphClosed: true, auditComplete: true,
      drained: true, missingReason: null, staleSamplesUpper: 0, stopConfirmationRecorded: true})
    expect(result.elapsedMs).toBeGreaterThanOrEqual(0)
  })
}
