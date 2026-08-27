import { defineConfig, devices } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const fixture = join(dirname(fileURLToPath(import.meta.url)), 'playwright', 'fixtures', 'speech.wav')

export default defineConfig({
  testDir: './integration/livekit',
  fullyParallel: false,
  timeout: 30_000,
  use: {
    baseURL: process.env.LIVEKIT_TEST_FRONTEND_URL,
    permissions: ['microphone'],
    launchOptions: {
      args: [
        '--use-fake-device-for-media-stream',
        '--use-fake-ui-for-media-stream',
        `--use-file-for-fake-audio-capture=${fixture}`,
      ],
    },
  },
  projects: [{
    name: 'livekit/chromium',
    use: { ...devices['Desktop Chrome'] },
  }],
})
