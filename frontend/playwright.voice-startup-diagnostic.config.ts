import { defineConfig, type PlaywrightTestConfig } from '@playwright/test'
import { resolve } from 'node:path'
import { createSuiteConfig } from './playwright/suite-config'

// HTTPの一部を制御する局所診断。通常の実接続受入とは収集・成果物を分離する。
const base = createSuiteConfig('integration-voice')
const runId = process.env.VOICE_STARTUP_RUN_ID ?? 'standalone'
if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(runId)) {
  throw new Error('VOICE_STARTUP_RUN_ID must be a single safe identifier')
}
const root = resolve('test-results/voice-startup-diagnostic', runId)
const env = {
  DS_PROFILE: 'integration-voice', DS_ENVIRONMENT_ID: 'test',
  DS_DATA_DIR: root + '/runtime-data',
  DS_PROFILE_REPORT: root + '/runtime-data/runtime/standalone/resolved-profile.json',
  DS_ENVIRONMENT_RUN_REPORT: root + '/runtime-data/runtime/standalone/environment-run.json',
}
Object.assign(process.env, env)
const server = base.webServer as Exclude<PlaywrightTestConfig['webServer'], undefined | unknown[]>
export default defineConfig({
  ...base,
  testDir: './diagnostics/voice-startup',
  projects: base.projects?.map(project => ({ ...project, name: 'voice-startup-diagnostic/chromium' })),
  outputDir: root + '/playwright-artifacts',
  fullyParallel: false,
  workers: 1,
  reporter: process.argv.includes('--list') ? [['list']] : [
    ['list'], ['json', { outputFile: root + '/playwright-results.json' }],
  ],
  webServer: { ...server, env: { ...server.env, ...env } },
})
