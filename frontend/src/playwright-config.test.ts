import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { pathToFileURL } from 'node:url'

import { afterEach, describe, expect, test, vi } from 'vitest'

const playwrightConfigSourcePath = join(process.cwd(), 'playwright.config.ts')
const originalProfileReport = process.env.DS_PROFILE_REPORT
const originalEnvironmentRunReport = process.env.DS_ENVIRONMENT_RUN_REPORT
const originalEnvironmentReadyUrl = process.env.DS_ENVIRONMENT_READY_URL

const restoreEnv = (envName: string, value: string | undefined) => {
  if (value === undefined) {
    delete process.env[envName]
    return
  }

  process.env[envName] = value
}

const loadPlaywrightConfig = async (argv: string[]) => {
  process.argv = argv
  vi.resetModules()
  const configUrl = `${pathToFileURL(playwrightConfigSourcePath).href}?test=${randomUUID()}`
  const module = await import(configUrl)

  return module.default as {
    testDir?: unknown
    webServer?: unknown
    workers?: unknown
    reporter?: unknown
  }
}

describe('Playwright voice chat webServer configuration', () => {
  afterEach(() => {
    process.argv = ['node', 'playwright', 'test']
    vi.unstubAllEnvs()
    restoreEnv('DS_PROFILE_REPORT', originalProfileReport)
    restoreEnv('DS_ENVIRONMENT_RUN_REPORT', originalEnvironmentRunReport)
    restoreEnv('DS_ENVIRONMENT_READY_URL', originalEnvironmentReadyUrl)
    vi.resetModules()
  })

  test('delegates backend mode resolution to the startup boundary', async () => {
    const config = await loadPlaywrightConfig(['node', 'playwright', 'test'])

    expect(config.workers).toBeUndefined()
    expect(config.webServer).toEqual(
      expect.objectContaining({
        command: '../scripts/start-voice-chat-e2e.sh',
        reuseExistingServer: false,
        timeout: 600_000,
        url: 'http://127.0.0.1:4174/ready',
      }),
    )
  })

  test('waits for the configured all-services readiness gate', async () => {
    vi.stubEnv('DS_ENVIRONMENT_READY_URL', 'http://127.0.0.1:4317/all-ready')

    const config = await loadPlaywrightConfig(['node', 'playwright', 'test'])

    expect(config.webServer).toEqual(
      expect.objectContaining({ url: 'http://127.0.0.1:4317/all-ready' }),
    )
  })

  test('does not branch on voice backend mode in Playwright config', async () => {
    vi.stubEnv('VOICE_CHAT_E2E_BACKEND', 'mock')

    const config = await loadPlaywrightConfig(['node', 'playwright', 'test'])

    expect(config.webServer).toEqual(
      expect.objectContaining({
        command: '../scripts/start-voice-chat-e2e.sh',
        reuseExistingServer: false,
        timeout: 600_000,
      }),
    )
  })

  test('keeps a single startup boundary when chat backend mode is set for voice chat spec', async () => {
    vi.stubEnv('CHAT_E2E_BACKEND', 'real')

    const config = await loadPlaywrightConfig([
      'node',
      'playwright',
      'test',
      'frontend/e2e/voice-chat.spec.ts',
    ])

    expect(config.workers).toBeUndefined()
    expect(config.webServer).toEqual(
      expect.objectContaining({
        command: '../scripts/start-voice-chat-e2e.sh',
        reuseExistingServer: false,
        timeout: 600_000,
      }),
    )
  })

  test('keeps a single startup boundary for full e2e runs', async () => {
    vi.stubEnv('CHAT_E2E_BACKEND', 'real')

    const config = await loadPlaywrightConfig(['node', 'playwright', 'test', 'frontend/e2e'])

    expect(config.workers).toBeUndefined()
    expect(config.webServer).toEqual(
      expect.objectContaining({
        command: '../scripts/start-voice-chat-e2e.sh',
        reuseExistingServer: false,
        timeout: 600_000,
      }),
    )
  })

  test('sets resolved profile report path for the startup boundary and specs', async () => {
    await loadPlaywrightConfig(['node', 'playwright', 'test'])

    expect(process.env.DS_PROFILE_REPORT).toBe(
      join(process.cwd(), 'test-results', 'resolved-profile.json'),
    )
  })

  test('preserves explicit resolved profile report path', async () => {
    vi.stubEnv('DS_PROFILE_REPORT', '/tmp/custom-profile-report.json')

    await loadPlaywrightConfig(['node', 'playwright', 'test'])

    expect(process.env.DS_PROFILE_REPORT).toBe('/tmp/custom-profile-report.json')
  })

  test('sets environment run report path for the shared startup boundary', async () => {
    const config = await loadPlaywrightConfig(['node', 'playwright', 'test'])

    expect(process.env.DS_ENVIRONMENT_RUN_REPORT).toBe(
      join(process.cwd(), 'test-results', 'environment-run.json'),
    )
    expect(config.reporter).toEqual([
      ['list'],
      ['./e2e/environment-run-reporter.ts'],
    ])
  })

  test('preserves explicit environment run report path', async () => {
    vi.stubEnv('DS_ENVIRONMENT_RUN_REPORT', '/tmp/custom-environment-run.json')

    await loadPlaywrightConfig(['node', 'playwright', 'test'])

    expect(process.env.DS_ENVIRONMENT_RUN_REPORT).toBe('/tmp/custom-environment-run.json')
  })

})
