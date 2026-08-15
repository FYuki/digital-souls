import { randomUUID } from 'node:crypto'
import { readFile } from 'node:fs/promises'
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
const injectedFrontendBaseUrl = 'http://127.0.0.1:25173'
const injectedReadyGateBaseUrl = 'http://localhost:24174'

const createProfileLoader = (readyGateBaseUrl = injectedReadyGateBaseUrl) => vi.fn(() => ({
  readyGate: { baseUrl: readyGateBaseUrl },
  dependencies: {
    frontend: { baseUrl: injectedFrontendBaseUrl },
  },
}))

const loadConfig = async (fileName: string) => {
  vi.resetModules()
  const sourcePath = join(process.cwd(), fileName)
  const configUrl = `${pathToFileURL(sourcePath).href}?test=${randomUUID()}`
  const module = await import(configUrl)
  return module.default as {
    testDir?: string
    outputDir?: string
    reporter?: Array<[string, Record<string, unknown>?]>
    use?: { baseURL?: string }
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
    vi.stubEnv('DS_ENVIRONMENT_ID', 'dogfood')
    vi.stubEnv('DS_DATA_DIR', '/tmp/caller-runtime-data')
    vi.stubEnv('DS_PROFILE_REPORT', '/tmp/caller-profile.json')
    vi.stubEnv('DS_ENVIRONMENT_RUN_REPORT', '/tmp/caller-environment.json')

    const config = await loadConfig(suite.config)
    const resultDir = join(process.cwd(), 'test-results', suite.suite)
    const dataRoot = join(process.cwd(), 'test-results', 'runtime-data', suite.suite)
    const runtimeDir = join(dataRoot, 'runtime', 'standalone')

    expect(config.testDir).toBe(suite.testDir)
    expect(config.outputDir).toBe(join(
      process.cwd(),
      'test-results',
      'playwright-artifacts',
      suite.suite,
    ))
    expect(process.env.DS_PROFILE).toBe(suite.profile)
    expect(process.env.DS_ENVIRONMENT_ID).toBe('test')
    expect(process.env.DS_DATA_DIR).toBe(dataRoot)
    expect(process.env.DS_PROFILE_REPORT).toBe(join(runtimeDir, 'resolved-profile.json'))
    expect(process.env.DS_ENVIRONMENT_RUN_REPORT).toBe(join(runtimeDir, 'environment-run.json'))
    expect(config.reporter).toEqual(expect.arrayContaining([
      ['json', { outputFile: join(resultDir, 'playwright-results.json') }],
      ['./playwright/suite-reporter-entrypoint.ts', {
        suite: suite.suite,
      }],
    ]))
  })

  test.each(suites)('$suite keeps the environment orchestrator attached without reusing a server', async (suite) => {
    const config = await loadConfig(suite.config)
    const profile = JSON.parse(await readFile(
      join(process.cwd(), '..', 'environments', 'profiles', `${suite.profile}.json`),
      'utf-8',
    )) as { readyGate: { baseUrl: string }, dependencies: { frontend: { baseUrl: string } } }
    const dataRoot = join(process.cwd(), 'test-results', 'runtime-data', suite.suite)
    const runtimeDir = join(dataRoot, 'runtime', 'standalone')

    expect(config.webServer).toEqual(expect.objectContaining({
      command: 'python3 ../environments/environment_cli.py up',
      env: {
        DS_PROFILE: suite.profile,
        DS_ENVIRONMENT_ID: 'test',
        DS_DATA_DIR: dataRoot,
        DS_PROFILE_REPORT: join(runtimeDir, 'resolved-profile.json'),
        DS_ENVIRONMENT_RUN_REPORT: join(runtimeDir, 'environment-run.json'),
      },
      reuseExistingServer: false,
      timeout: 600_000,
      gracefulShutdown: { signal: 'SIGTERM', timeout: 60_000 },
    }))
    expect(config.use?.baseURL).toBe(profile.dependencies.frontend.baseUrl)
    expect(config.webServer?.url).toBe(`${profile.readyGate.baseUrl}/ready`)
  })

  test('should use the selected profile as the only source of the browser base URL', () => {
    const loadProfile = createProfileLoader()

    const config = createSuiteConfig('mocked-e2e', { loadProfile })

    expect(config.use?.baseURL).toBe(injectedFrontendBaseUrl)
    expect(loadProfile).toHaveBeenCalledOnce()
    expect(loadProfile).toHaveBeenCalledWith('test-mocked')
  })

  test.each([
    injectedReadyGateBaseUrl,
    `${injectedReadyGateBaseUrl}/`,
  ])('should resolve the ready gate path when the profile origin is %s', (readyGateBaseUrl) => {
    const loadProfile = createProfileLoader(readyGateBaseUrl)

    const config = createSuiteConfig('mocked-e2e', { loadProfile })

    expect(Array.isArray(config.webServer)).toBe(false)
    const webServer = Array.isArray(config.webServer) ? undefined : config.webServer
    expect(webServer?.url).toBe('http://localhost:24174/ready')
    expect(loadProfile).toHaveBeenCalledOnce()
    expect(loadProfile).toHaveBeenCalledWith('test-mocked')
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
