import { randomUUID } from 'node:crypto'
import { join, relative } from 'node:path'
import { pathToFileURL } from 'node:url'

import { afterEach, describe, expect, test, vi } from 'vitest'

import { createSuiteConfig, type SuiteName } from '../playwright/suite-config'

type SuiteExpectation = {
  config: string
  suite: string
  profile: string
  testDir: string
}

const suites: SuiteExpectation[] = [
  {
    config: 'playwright.mocked.config.ts',
    suite: 'mocked-e2e',
    profile: 'test-mocked',
    testDir: './e2e',
  },
  {
    config: 'playwright.integration-text.config.ts',
    suite: 'integration-text',
    profile: 'integration-text',
    testDir: './integration/text',
  },
  {
    config: 'playwright.integration-voice.config.ts',
    suite: 'integration-voice',
    profile: 'integration-voice',
    testDir: './integration/voice',
  },
]

const originalArgv = [...process.argv]

const loadConfig = async (fileName: string) => {
  vi.resetModules()
  const sourcePath = join(process.cwd(), fileName)
  const configUrl = `${pathToFileURL(sourcePath).href}?test=${randomUUID()}`
  const module = await import(configUrl)
  return module.default as {
    testDir?: string
    outputDir?: string
    reporter?: Array<[string, Record<string, unknown>?]>
    webServer?: Record<string, unknown>
  }
}

describe('suite-specific Playwright configuration', () => {
  afterEach(() => {
    process.argv = [...originalArgv]
    vi.unstubAllEnvs()
    vi.resetModules()
  })

  test.each(suites)('$suite fixes its profile, collection root, and report paths', async (suite) => {
    vi.stubEnv('DS_PROFILE', 'caller-must-not-select-a-different-profile')
    vi.stubEnv('DS_PROFILE_REPORT', '/tmp/caller-profile.json')
    vi.stubEnv('DS_ENVIRONMENT_RUN_REPORT', '/tmp/caller-environment.json')

    const config = await loadConfig(suite.config)
    const resultDir = join(process.cwd(), 'test-results', suite.suite)

    expect(config.testDir).toBe(suite.testDir)
    expect(config.outputDir).toBe(join(
      process.cwd(),
      'test-results',
      'playwright-artifacts',
      suite.suite,
    ))
    expect(process.env.DS_PROFILE).toBe(suite.profile)
    expect(process.env.DS_PROFILE_REPORT).toBe(join(resultDir, 'resolved-profile.json'))
    expect(process.env.DS_ENVIRONMENT_RUN_REPORT).toBe(join(resultDir, 'environment-run.json'))
    expect(config.reporter).toEqual(expect.arrayContaining([
      ['json', { outputFile: join(resultDir, 'playwright-results.json') }],
      ['./playwright/suite-reporter.ts', {
        suite: suite.suite,
      }],
    ]))
  })

  test.each(suites)('$suite uses the environment startup boundary without reusing a server', async (suite) => {
    const config = await loadConfig(suite.config)

    expect(config.webServer).toEqual(expect.objectContaining({
      command: '../environments/up.sh',
      reuseExistingServer: false,
      timeout: 600_000,
      gracefulShutdown: { signal: 'SIGTERM', timeout: 60_000 },
    }))
  })

  test.each(suites)('$suite disables state-changing reporters in collection mode', async (suite) => {
    process.argv = [...originalArgv, '--list']

    const config = await loadConfig(suite.config)

    expect(config.reporter).toEqual([['list']])
  })

  test('the three suites have pairwise-disjoint collection roots and result directories', () => {
    expect(new Set(suites.map(({ testDir }) => testDir)).size).toBe(suites.length)
    expect(new Set(suites.map(({ suite }) => join('test-results', suite))).size).toBe(suites.length)
  })

  test.each(suites)('$suite keeps lifecycle reports outside Playwright cleanup roots', async (suite) => {
    const config = await loadConfig(suite.config)
    const resultDir = join(process.cwd(), 'test-results', suite.suite)

    expect(relative(resultDir, config.outputDir ?? '')).toMatch(/^\.\./)
  })

  test('rejects an unknown suite instead of accepting an inconsistent contract', () => {
    expect(() => createSuiteConfig('unknown-suite' as SuiteName))
      .toThrow(/unknown Playwright suite/i)
  })
})
