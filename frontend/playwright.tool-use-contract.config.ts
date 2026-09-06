import { defineConfig, devices } from '@playwright/test'
import { join } from 'node:path'

const baseURL = process.env.TOOL_USE_TEST_FRONTEND_URL
if (!baseURL) throw new Error('scripts/acceptance_tool_use.py --contract-mcpから起動してください')

export default defineConfig({
  testDir: './integration/tool-use-contract',
  outputDir: './test-results/tool-use-contract',
  workers: 1,
  timeout: 240_000,
  expect: { timeout: 120_000 },
  reporter: [['list'], ['json', { outputFile: 'test-results/tool-use-contract/results.json' }]],
  use: { ...devices['Desktop Chrome'], baseURL, permissions: ['microphone'], screenshot: 'only-on-failure' },
  projects: [
    { name: 'text', grep: /テキスト/ },
    { name: 'voice', grep: /音声/, use: { launchOptions: { args: [
      '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
      `--use-file-for-fake-audio-capture=${join(process.env.TOOL_USE_TEST_AUDIO_DIR!, 'mrtr.wav')}`,
    ] } } },
  ],
})
