import { defineConfig, devices } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { PROFILE_REPORT_ENV } from './e2e/resolved-profile'

const configDir = dirname(fileURLToPath(import.meta.url))
const ENVIRONMENT_RUN_REPORT_ENV = 'DS_ENVIRONMENT_RUN_REPORT'
const ENVIRONMENT_READY_URL_ENV = 'DS_ENVIRONMENT_READY_URL'
const DEFAULT_ENVIRONMENT_READY_URL = 'http://127.0.0.1:4174/ready'

const configuredReadyGateUrl = process.env[ENVIRONMENT_READY_URL_ENV]
const environmentReadyGateUrl =
  configuredReadyGateUrl === undefined || configuredReadyGateUrl.length === 0
    ? DEFAULT_ENVIRONMENT_READY_URL
    : configuredReadyGateUrl

const configuredProfileReport = process.env[PROFILE_REPORT_ENV]
if (configuredProfileReport === undefined || configuredProfileReport.length === 0) {
  process.env[PROFILE_REPORT_ENV] = join(configDir, 'test-results', 'resolved-profile.json')
}

const configuredEnvironmentRunReport = process.env[ENVIRONMENT_RUN_REPORT_ENV]
if (configuredEnvironmentRunReport === undefined || configuredEnvironmentRunReport.length === 0) {
  process.env[ENVIRONMENT_RUN_REPORT_ENV] = join(
    configDir,
    'test-results',
    'environment-run.json',
  )
}

const e2eWebServer = {
  command: '../scripts/start-voice-chat-e2e.sh',
  url: environmentReadyGateUrl,
  reuseExistingServer: false,
  timeout: 600_000,
  gracefulShutdown: {
    signal: 'SIGTERM' as const,
    timeout: 60_000,
  },
}

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  reporter: [['list'], ['./e2e/environment-run-reporter.ts']],
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  webServer: e2eWebServer,
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
