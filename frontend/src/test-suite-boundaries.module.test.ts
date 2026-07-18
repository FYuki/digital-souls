import { execFile } from 'node:child_process'
import { existsSync } from 'node:fs'
import { readdir, readFile } from 'node:fs/promises'
import { dirname, join, relative, resolve } from 'node:path'
import { promisify } from 'node:util'

import { describe, expect, test } from 'vitest'

const execFileAsync = promisify(execFile)

const suiteCases = [
  {
    config: 'playwright.mocked.config.ts',
    directory: 'e2e',
    capability: 'mocked-e2e',
    suite: 'mocked-e2e',
  },
  {
    config: 'playwright.integration-text.config.ts',
    directory: 'integration/text',
    capability: 'text-chat-real',
    suite: 'integration-text',
  },
  {
    config: 'playwright.integration-voice.config.ts',
    directory: 'integration/voice',
    capability: 'voice-chat-real',
    suite: 'integration-voice',
  },
] as const

const findSpecs = async (directory: string): Promise<string[]> => {
  const root = join(process.cwd(), directory)
  const entries = await readdir(root, { recursive: true, withFileTypes: true })
  return entries
    .filter((entry) => entry.isFile() && entry.name.endsWith('.spec.ts'))
    .map((entry) => join(entry.parentPath, entry.name))
    .sort()
}

const readResultSnapshot = async (suite: string): Promise<Map<string, string>> => {
  const root = join(process.cwd(), 'test-results', suite)
  if (!existsSync(root)) return new Map()

  const entries = await readdir(root, { recursive: true, withFileTypes: true })
  const files = entries
    .filter((entry) => entry.isFile())
    .map((entry) => join(entry.parentPath, entry.name))
    .sort()
  return new Map(await Promise.all(files.map(async (file) => [
    relative(root, file),
    await readFile(file, 'utf-8'),
  ] as const)))
}

const findReachableTypescriptSources = async (entries: string[]): Promise<Map<string, string>> => {
  const pending = [...entries]
  const sources = new Map<string, string>()

  while (pending.length > 0) {
    const sourcePath = pending.shift()
    if (sourcePath === undefined || sources.has(sourcePath)) continue
    const source = await readFile(sourcePath, 'utf-8')
    sources.set(sourcePath, source)

    const relativeImports = [...source.matchAll(/from\s+['"](\.[^'"]+)['"]/g)]
      .map((match) => match[1])
    pending.push(...relativeImports.map((specifier) => (
      resolve(dirname(sourcePath), specifier.endsWith('.ts') ? specifier : `${specifier}.ts`)
    )))
  }

  return sources
}

describe('Playwright suite boundaries', () => {
  test.each(suiteCases)('$directory specs require only $capability', async ({ directory, capability }) => {
    const specs = await findSpecs(directory)
    expect(specs.length).toBeGreaterThan(0)

    for (const spec of specs) {
      const source = await readFile(spec, 'utf-8')
      const requiredCapabilities = [...source.matchAll(
        /getCapabilitySkipReason\s*\(\s*resolvedProfile\s*,\s*['"]([^'"]+)['"]\s*\)/g,
      )].map((match) => match[1])
      expect(requiredCapabilities, relative(process.cwd(), spec)).toEqual([capability])
    }
  })

  test.each(suiteCases)('$config collects only specs from $directory', async ({ config, directory }) => {
    const executable = join(process.cwd(), 'node_modules', '.bin', 'playwright')
    const { stdout } = await execFileAsync(
      executable,
      ['test', '--list', '--config', config],
      { cwd: process.cwd() },
    )
    const collectedLines = stdout.split('\n').filter((line) => line.includes('.spec.ts:'))

    expect(collectedLines.length).toBeGreaterThan(0)
    expect(collectedLines.every((line) => line.includes(`${directory}/`))).toBe(true)
    for (const otherSuite of suiteCases.filter((candidate) => candidate.directory !== directory)) {
      expect(collectedLines.some((line) => line.includes(`${otherSuite.directory}/`))).toBe(false)
    }
  }, 20_000)

  test.each(suiteCases)('$config does not change $suite results when collecting specs', async ({ config, suite }) => {
    const executable = join(process.cwd(), 'node_modules', '.bin', 'playwright')
    const before = await readResultSnapshot(suite)

    await execFileAsync(
      executable,
      ['test', '--list', '--config', config],
      { cwd: process.cwd() },
    )

    expect(await readResultSnapshot(suite)).toEqual(before)
  }, 20_000)

  test.each(['integration/text', 'integration/voice'])('%s reachable helpers do not replace external communication', async (directory) => {
    const specs = await findSpecs(directory)
    const sources = await findReachableTypescriptSources(specs)

    for (const [sourcePath, source] of sources) {
      expect(relative(process.cwd(), sourcePath)).not.toMatch(/^e2e\//)
      expect(source).not.toMatch(/mock-web-socket|installMockWebSocketBackend/)
      expect(source).not.toMatch(/\bpage\.route\s*\(|routeFromHAR\s*\(/)
    }
  })

  test.each(suiteCases)('$directory sources do not depend on another suite layer', async ({ directory }) => {
    const specs = await findSpecs(directory)
    const sources = await findReachableTypescriptSources(specs)
    const forbiddenRoots = suiteCases
      .map((suite) => suite.directory)
      .filter((candidate) => candidate !== directory)

    for (const sourcePath of sources.keys()) {
      const sourceName = relative(process.cwd(), sourcePath)
      expect(
        forbiddenRoots.some((root) => sourceName === root || sourceName.startsWith(`${root}/`)),
        sourceName,
      ).toBe(false)
    }
  })

  test.each(suiteCases)('$directory specs do not switch layers from environment or dependency mode', async ({ directory }) => {
    const specs = await findSpecs(directory)
    const sources = await Promise.all(specs.map((spec) => readFile(spec, 'utf-8')))

    for (const source of sources) {
      expect(source).not.toMatch(/CHAT_E2E_BACKEND|VOICE_CHAT_E2E_BACKEND|DS_PROFILE/)
      expect(source).not.toMatch(/dependencies\.backend\.mode|process\.env/)
    }
  })
})
