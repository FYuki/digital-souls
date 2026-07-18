import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

import { describe, expect, test } from 'vitest'

type PackageManifest = { scripts?: Record<string, string> }

const readJson = async <T>(path: string): Promise<T> => JSON.parse(
  await readFile(path, 'utf-8'),
) as T

describe('test execution entrypoints', () => {
  test('the repository exposes stable and unambiguous test commands', async () => {
    const manifest = await readJson<PackageManifest>(join(process.cwd(), '..', 'package.json'))

    expect(manifest.scripts).toEqual(expect.objectContaining({
      'test:unit': expect.stringMatching(/backend\/tests\/unit/),
      'test:module': expect.stringMatching(/backend\/tests\/module/),
      'test:integration:backend': expect.stringMatching(/backend\/tests\/integration/),
      'test:e2e:mocked': expect.stringContaining('test:e2e:mocked'),
      'test:integration:text': expect.stringContaining('test:integration:text'),
      'test:integration:voice': expect.stringContaining('test:integration:voice'),
    }))
  })

  test('frontend commands select exactly one naming pattern or Playwright config', async () => {
    const manifest = await readJson<PackageManifest>(join(process.cwd(), 'package.json'))

    expect(manifest.scripts).toEqual(expect.objectContaining({
      'test:unit': expect.stringMatching(/\.unit\.test\.ts/),
      'test:module': expect.stringMatching(/\.module\.test\.ts/),
      'test:e2e:mocked': expect.stringContaining('playwright.mocked.config.ts'),
      'test:integration:text': expect.stringContaining('playwright.integration-text.config.ts'),
      'test:integration:voice': expect.stringContaining('playwright.integration-voice.config.ts'),
    }))
    expect(manifest.scripts).not.toHaveProperty('test:e2e')
  })

  test('Vitest excludes both kinds of Playwright tests', async () => {
    const source = await readFile(join(process.cwd(), 'vite.config.ts'), 'utf-8')

    expect(source).toContain("'./e2e/**'")
    expect(source).toContain("'./integration/**'")
  })

  test('CI runs local suites but never starts real-service integration suites', async () => {
    const source = await readFile(join(process.cwd(), '..', '.github', 'workflows', 'ci.yml'), 'utf-8')

    expect(source).toMatch(/npm run test:unit/)
    expect(source).toMatch(/npm run test:module/)
    expect(source).toMatch(/npm run test:e2e:mocked/)
    expect(source).not.toMatch(/npm run test:integration:(backend|text|voice)/)
  })

  test('the CI check entrypoint includes strict Backend and Frontend type checks', async () => {
    const manifest = await readJson<PackageManifest>(join(process.cwd(), '..', 'package.json'))
    const check = manifest.scripts?.check

    expect(check).toMatch(
      /python3 -m mypy --config-file backend\/mypy\.ini backend\/app environments/,
    )
    expect(check).toMatch(/npm --prefix frontend run check/)
  })
})
