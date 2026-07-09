import { join } from 'node:path'
import { randomUUID } from 'node:crypto'
import { pathToFileURL } from 'node:url'

import { afterEach, describe, expect, test, vi } from 'vitest'

const playwrightConfigSourcePath = join(process.cwd(), 'playwright.config.ts')
const originalVoiceChatBackendReport = process.env.VOICE_CHAT_E2E_BACKEND_REPORT

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

  return module.default as { testDir?: unknown; webServer?: unknown; workers?: unknown }
}

describe('Playwright voice chat webServer configuration', () => {
  afterEach(() => {
    process.argv = ['node', 'playwright', 'test']
    vi.unstubAllEnvs()
    restoreEnv('VOICE_CHAT_E2E_BACKEND_REPORT', originalVoiceChatBackendReport)
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
      }),
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

  test('sets voice backend report path for the startup boundary and specs', async () => {
    await loadPlaywrightConfig(['node', 'playwright', 'test'])

    expect(process.env.VOICE_CHAT_E2E_BACKEND_REPORT).toBe(
      join(process.cwd(), 'test-results', 'voice-chat-backend.json'),
    )
  })

  test('does not inject chat backend report path globally', async () => {
    vi.stubEnv('CHAT_E2E_BACKEND_REPORT', undefined)

    await loadPlaywrightConfig(['node', 'playwright', 'test'])

    expect(process.env.CHAT_E2E_BACKEND_REPORT).toBeUndefined()
  })

  test('preserves explicit voice backend report path', async () => {
    vi.stubEnv('VOICE_CHAT_E2E_BACKEND_REPORT', '/tmp/custom-voice-report.json')

    await loadPlaywrightConfig(['node', 'playwright', 'test'])

    expect(process.env.VOICE_CHAT_E2E_BACKEND_REPORT).toBe('/tmp/custom-voice-report.json')
  })

})
