import { defineConfig, type PlaywrightTestConfig } from '@playwright/test'
import { join } from 'node:path'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createSuiteConfig } from './playwright/suite-config'
import { PROFILE_REPORT_ENV } from './resolved-profile'

const frontendRoot = dirname(fileURLToPath(import.meta.url))
const controlledRoot = join(frontendRoot, 'test-results', 'controlled-baseline')
const dataRoot = join(controlledRoot, 'runtime-data')
const tracePath = join(dataRoot, 'voice-metrics', 'controlled-trace.jsonl')
const manifestPath = join(controlledRoot, 'trial-manifest.json')
const base = createSuiteConfig('integration-voice')
const isCollectionOnly = process.argv.includes('--list')
const webServer = base.webServer as NonNullable<PlaywrightTestConfig['webServer']> & {
  env: Record<string, string>
}

Object.assign(process.env, {
  DS_DATA_DIR: dataRoot,
  DS_ENVIRONMENT_ID: 'test',
  DS_ENVIRONMENT_RUN_REPORT: join(dataRoot, 'runtime', 'standalone', 'environment-run.json'),
  [PROFILE_REPORT_ENV]: join(dataRoot, 'runtime', 'standalone', 'resolved-profile.json'),
  VOICE_MEASUREMENT_KIND: 'controlled_baseline',
  VOICE_CONTROLLED_TRACE_PATH: tracePath,
  VOICE_BASELINE_MANIFEST_PATH: manifestPath,
})

export default defineConfig({
  ...base,
  testDir: './integration/voice-baseline',
  outputDir: join(controlledRoot, 'playwright-artifacts'),
  fullyParallel: false,
  projects: base.projects?.map((project) => ({
    ...project,
    name: 'controlled-baseline/chromium',
  })),
  reporter: isCollectionOnly
    ? [['list']]
    : [
        ['list'],
        ['json', { outputFile: join(controlledRoot, 'playwright-results.json') }],
      ],
  webServer: {
    ...webServer,
    env: {
      ...webServer.env,
      DS_DATA_DIR: dataRoot,
      DS_ENVIRONMENT_ID: 'test',
      DS_ENVIRONMENT_RUN_REPORT: join(dataRoot, 'runtime', 'standalone', 'environment-run.json'),
      [PROFILE_REPORT_ENV]: join(dataRoot, 'runtime', 'standalone', 'resolved-profile.json'),
      VOICE_MEASUREMENT_KIND: 'controlled_baseline',
      VOICE_CONTROLLED_TRACE_PATH: tracePath,
    },
  },
})
