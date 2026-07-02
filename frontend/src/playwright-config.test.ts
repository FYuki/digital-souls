import { randomUUID } from 'node:crypto'
import { pathToFileURL } from 'node:url'
import { join } from 'node:path'

import { afterEach, describe, expect, test, vi } from 'vitest'

const playwrightConfigSourcePath = join(process.cwd(), 'playwright.config.ts')

const loadPlaywrightConfig = async () => {
  vi.resetModules()
  const configUrl = `${pathToFileURL(playwrightConfigSourcePath).href}?test=${randomUUID()}`
  const module = await import(configUrl)

  return module.default as { webServer?: unknown }
}

describe('Playwright voice chat webServer configuration', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.resetModules()
  })

  test('uses the voice chat E2E startup boundary by default', async () => {
    const config = await loadPlaywrightConfig()

    expect(config.webServer).toEqual(
      expect.objectContaining({
        command: '../scripts/start-voice-chat-e2e.sh',
        reuseExistingServer: false,
        timeout: 600_000,
      }),
    )
  })

  test('does not duplicate voice backend mode branching in Playwright config', async () => {
    vi.stubEnv('VOICE_CHAT_E2E_BACKEND', 'mock')

    const config = await loadPlaywrightConfig()

    expect(config.webServer).toEqual(
      expect.objectContaining({
        command: '../scripts/start-voice-chat-e2e.sh',
        reuseExistingServer: false,
        timeout: 600_000,
      }),
    )
  })
})
