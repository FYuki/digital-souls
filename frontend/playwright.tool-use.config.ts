import { defineConfig, devices } from '@playwright/test'
import { join } from 'node:path'

const baseURL = process.env.TOOL_USE_TEST_FRONTEND_URL
const outputDir = process.env.TOOL_USE_TEST_RESULTS_DIR
if (!baseURL || !outputDir) throw new Error('scripts/acceptance_tool_use.pyから起動してください')

export default defineConfig({
  forbidOnly: true,
  testDir: './integration/tool-use',
  outputDir,
  fullyParallel: false,
  workers: 1,
  timeout: 240_000,
  expect: { timeout: 120_000 },
  reporter: [['list'], ['json', { outputFile: join(outputDir, 'results.json') }]],
  use: { ...devices['Desktop Chrome'], baseURL, permissions: ['microphone'], screenshot: 'only-on-failure' },
  projects: [
    { name: 'text', grep: /実ブラウザ/ },
    ...(['filesystem', 'resource'] as const).map(kind => ({
      name: kind, grep: new RegExp(`実LiveKit音声/${kind}`),
      use: { launchOptions: { args: [
        '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
        `--use-file-for-fake-audio-capture=${join(process.env.TOOL_USE_TEST_AUDIO_DIR!, `${kind}.wav`)}`,
      ] } },
    })),
  ],
})
