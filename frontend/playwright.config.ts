import { defineConfig, devices } from '@playwright/test'

const frontendWebServer = {
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
  webServer: frontendWebServer,
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
