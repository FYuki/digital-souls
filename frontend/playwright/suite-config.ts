import { defineConfig, devices, type PlaywrightTestConfig } from '@playwright/test'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { PROFILE_REPORT_ENV } from '../resolved-profile'

const configDir = dirname(fileURLToPath(import.meta.url))
const frontendDir = join(configDir, '..')

export const ENVIRONMENT_RUN_REPORT_ENV = 'DS_ENVIRONMENT_RUN_REPORT'
export const PROFILE_ENV = 'DS_PROFILE'
export const ENVIRONMENT_ID_ENV = 'DS_ENVIRONMENT_ID'
export const DATA_DIR_ENV = 'DS_DATA_DIR'

export type SuiteName = 'mocked-e2e' | 'integration-text' | 'integration-voice'
type TestLayer = 'e2e' | 'integration'
type ProfileName = 'test-mocked' | 'integration-text' | 'integration-voice'

export type SuiteDefinition = Readonly<{
  suite: SuiteName
  testLayer: TestLayer
  profile: ProfileName
  testDir: string
  resultDir: string
  artifactDir: string
  dataRoot: string
  runtimeDir: string
}>

const suiteDefinitions: Readonly<Record<SuiteName, Readonly<{
  testLayer: TestLayer
  profile: ProfileName
  testDir: string
}>>> = Object.freeze({
  'mocked-e2e': Object.freeze({
    testLayer: 'e2e',
    profile: 'test-mocked',
    testDir: './e2e',
  }),
  'integration-text': Object.freeze({
    testLayer: 'integration',
    profile: 'integration-text',
    testDir: './integration/text',
  }),
  'integration-voice': Object.freeze({
    testLayer: 'integration',
    profile: 'integration-voice',
    testDir: './integration/voice',
  }),
})

export const getSuiteDefinition = (suite: SuiteName): SuiteDefinition => {
  const definition = suiteDefinitions[suite]
  if (definition === undefined) {
    throw new Error(`unknown Playwright suite: ${String(suite)}`)
  }
  const dataRoot = join(frontendDir, 'test-results', 'runtime-data', suite)
  return Object.freeze({
    suite,
    ...definition,
    resultDir: join(frontendDir, 'test-results', suite),
    artifactDir: join(frontendDir, 'test-results', 'playwright-artifacts', suite),
    dataRoot,
    runtimeDir: join(dataRoot, 'runtime', 'standalone'),
  })
}

export const createSuiteConfig = (suiteName: SuiteName): PlaywrightTestConfig => {
  const suite = getSuiteDefinition(suiteName)
  const isCollectionOnly = process.argv.includes('--list')
  const profileReportPath = join(suite.runtimeDir, 'resolved-profile.json')
  const environmentRunReportPath = join(suite.runtimeDir, 'environment-run.json')
  process.env[PROFILE_ENV] = suite.profile
  process.env[ENVIRONMENT_ID_ENV] = 'test'
  process.env[DATA_DIR_ENV] = suite.dataRoot
  process.env[PROFILE_REPORT_ENV] = profileReportPath
  process.env[ENVIRONMENT_RUN_REPORT_ENV] = environmentRunReportPath

  return defineConfig({
    testDir: suite.testDir,
    outputDir: suite.artifactDir,
    fullyParallel: true,
    reporter: isCollectionOnly
      ? [['list']]
      : [
          ['list'],
          ['json', { outputFile: join(suite.resultDir, 'playwright-results.json') }],
          ['./playwright/suite-reporter-entrypoint.ts', {
            suite: suite.suite,
          }],
        ],
    use: {
      baseURL: 'http://localhost:5173',
      trace: 'on-first-retry',
    },
    webServer: {
      command: '../environments/up.sh',
      env: {
        [PROFILE_ENV]: suite.profile,
        [ENVIRONMENT_ID_ENV]: 'test',
        [DATA_DIR_ENV]: suite.dataRoot,
        [PROFILE_REPORT_ENV]: profileReportPath,
        [ENVIRONMENT_RUN_REPORT_ENV]: environmentRunReportPath,
      },
      url: 'http://127.0.0.1:4174/ready',
      reuseExistingServer: false,
      timeout: 600_000,
      gracefulShutdown: { signal: 'SIGTERM', timeout: 60_000 },
    },
    projects: [{
      name: `${suite.testDir.replace(/^\.\//, '')}/chromium`,
      use: { ...devices['Desktop Chrome'] },
    }],
  })
}
