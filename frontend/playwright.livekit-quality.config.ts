import { readFileSync } from 'node:fs'
import { defineConfig, type PlaywrightTestConfig } from '@playwright/test'
import { join } from 'node:path'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { createSuiteConfig } from './playwright/suite-config'
import { PROFILE_REPORT_ENV } from './resolved-profile'

const frontendRoot = dirname(fileURLToPath(import.meta.url))
const controlledRoot = join(frontendRoot, 'test-results', 'livekit-quality')
const runId = process.env.VOICE_QUALITY_RUN_ID
if (runId !== undefined && !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(runId)) {
  throw new Error('VOICE_QUALITY_RUN_ID must be a single safe run identifier')
}
const resultRoot = runId === undefined ? controlledRoot : join(controlledRoot, 'runs', runId)
const dataRoot = join(resultRoot, 'runtime-data')
const tracePath = join(dataRoot, 'voice-metrics', 'controlled-trace.jsonl')
const manifestPath = join(resultRoot, 'trial-manifest.json')
const faultBridge = process.env.VOICE_QUALITY_FAULT_BRIDGE === '1'
if (process.env.VOICE_QUALITY_FAULT_BRIDGE !== undefined && !faultBridge) {
  throw new Error('invalid fault bridge selection')
}
if (faultBridge && process.env.VOICE_QUALITY_CONTROL_PROBE !== '1') {
  throw new Error('fault bridge requires an explicit control probe diagnostic')
}
if (process.env.VOICE_QUALITY_NETWORK_FAULT !== undefined
  && (process.env.VOICE_QUALITY_NETWORK_FAULT !== '1' || !faultBridge)) {
  throw new Error('network fault requires explicit dedicated bridge')
}
const pcmObserver = process.env.VOICE_QUALITY_OBSERVE_STT_PCM === '1'
if (process.env.VOICE_QUALITY_OBSERVE_STT_PCM !== undefined && (!pcmObserver || faultBridge)) {
  throw new Error('invalid PCM observer selection')
}
const selectedProfile = pcmObserver ? 'integration-voice-pcm' : (faultBridge ? 'integration-voice-fault' : 'integration-voice')
const base = createSuiteConfig('integration-voice', selectedProfile !== 'integration-voice' ? {
  loadProfile: () => JSON.parse(readFileSync(join(frontendRoot, '..', 'environments', 'profiles', `${selectedProfile}.json`), 'utf8')),
} : undefined)
const isCollectionOnly = process.argv.includes('--list')
const webServer = base.webServer as NonNullable<PlaywrightTestConfig['webServer']> & {
  env: Record<string, string>
}

Object.assign(process.env, {
  DS_PROFILE: selectedProfile,
  DS_DATA_DIR: dataRoot,
  DS_ENVIRONMENT_ID: 'test',
  DS_ENVIRONMENT_RUN_REPORT: join(dataRoot, 'runtime', 'standalone', 'environment-run.json'),
  [PROFILE_REPORT_ENV]: join(dataRoot, 'runtime', 'standalone', 'resolved-profile.json'),
  VOICE_MEASUREMENT_KIND: 'controlled_baseline',
  VOICE_CONTROLLED_TRACE_PATH: tracePath,
  VOICE_QUALITY_MANIFEST_PATH: manifestPath,
})

export default defineConfig({
  ...base,
  testDir: './integration/voice-quality',
  outputDir: join(resultRoot, 'playwright-artifacts'),
  fullyParallel: false,
  projects: base.projects?.map((project) => ({
    ...project,
    name: 'livekit-quality/chromium',
  })),
  reporter: isCollectionOnly
    ? [['list']]
    : [
        ['list'],
        ['json', { outputFile: join(resultRoot, 'playwright-results.json') }],
      ],
  webServer: {
    ...webServer,
    env: {
      ...webServer.env,
      DS_PROFILE: selectedProfile,
      DS_DATA_DIR: dataRoot,
      DS_ENVIRONMENT_ID: 'test',
      DS_ENVIRONMENT_RUN_REPORT: join(dataRoot, 'runtime', 'standalone', 'environment-run.json'),
      [PROFILE_REPORT_ENV]: join(dataRoot, 'runtime', 'standalone', 'resolved-profile.json'),
      VOICE_MEASUREMENT_KIND: 'controlled_baseline',
      VOICE_CONTROLLED_TRACE_PATH: tracePath,
    },
  },
})
