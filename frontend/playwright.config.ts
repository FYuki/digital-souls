import { defineConfig, devices } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { PROFILE_REPORT_ENV } from './e2e/resolved-profile'

const configDir = dirname(fileURLToPath(import.meta.url))

const configuredProfileReport = process.env[PROFILE_REPORT_ENV]
if (configuredProfileReport === undefined || configuredProfileReport.length === 0) {
  process.env[PROFILE_REPORT_ENV] = join(configDir, 'test-results', 'resolved-profile.json')
}

const e2eWebServer = {
  command: '../scripts/start-voice-chat-e2e.sh',
  url: 'http://localhost:5173',
  reuseExistingServer: false,
  timeout: 600_000,
}

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  reporter: 'list',
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
