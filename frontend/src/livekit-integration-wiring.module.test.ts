import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

import { describe, expect, test } from 'vitest'

type PackageManifest = { scripts?: Record<string, string> }

const readManifest = async (path: string): Promise<PackageManifest> => JSON.parse(
  await readFile(path, 'utf-8'),
) as PackageManifest

describe('LiveKit real-service suite wiring', () => {
  test('repository task runs both real Backend and browser suites', async () => {
    const manifest = await readManifest(join(process.cwd(), '..', 'package.json'))
    const command = manifest.scripts?.['test:integration:livekit']

    expect(command).toBeDefined()
    expect(command).toBe(
      'PYTHONPATH=backend python3 -m pytest backend/tests/integration/test_livekit_transport_integration.py'
      + ' && npm --prefix frontend run test:integration:livekit',
    )
  })

  test('frontend task selects a dedicated Playwright configuration', async () => {
    const manifest = await readManifest(join(process.cwd(), 'package.json'))
    const command = manifest.scripts?.['test:integration:livekit']

    expect(command).toBeDefined()
    expect(command).toBe(
      'playwright test --config playwright.integration-livekit.config.ts',
    )
  })

  test('CI does not run the opt-in LiveKit suite', async () => {
    const workflow = await readFile(
      join(process.cwd(), '..', '.github', 'workflows', 'ci.yml'),
      'utf-8',
    )

    expect(workflow).not.toMatch(/npm run test:integration:livekit/)
    expect(workflow).not.toContain('test_livekit_transport_integration.py')
    expect(workflow).not.toContain('playwright.integration-livekit.config.ts')
    expect(workflow).not.toContain('npm --prefix frontend run test:integration:livekit')
  })
})
