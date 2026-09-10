import { defineConfig, devices } from '@playwright/test'
import { join } from 'node:path'

const baseURL = process.env.TOOL_USE_TEST_FRONTEND_URL
const outputDir = process.env.TOOL_USE_TEST_RESULTS_DIR
if (!baseURL || !outputDir) throw new Error('scripts/acceptance_tool_use.py --addon-actionから起動してください')

export default defineConfig({
  forbidOnly: true,
  testDir: './integration/addon-action',
  outputDir,
  workers: 1,
  fullyParallel: false,
  timeout: 900_000,
  expect: { timeout: 180_000 },
  reporter: [['list'], ['json', { outputFile: join(outputDir, 'results.json') }]],
  use: {
    ...devices['Desktop Chrome'], baseURL, permissions: ['microphone'],
    screenshot: 'only-on-failure',
    launchOptions: { args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'] },
  },
})
