import { defineConfig, devices } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const configDir = dirname(fileURLToPath(import.meta.url))
const VOICE_CHAT_BACKEND_REPORT_ENV = 'VOICE_CHAT_E2E_BACKEND_REPORT'

const configuredVoiceBackendReport = process.env[VOICE_CHAT_BACKEND_REPORT_ENV]
if (configuredVoiceBackendReport === undefined || configuredVoiceBackendReport.length === 0) {
  process.env[VOICE_CHAT_BACKEND_REPORT_ENV] = join(configDir, 'test-results', 'voice-chat-backend.json')
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
