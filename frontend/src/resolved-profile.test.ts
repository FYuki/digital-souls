import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import {
  PROFILE_REPORT_ENV,
  attachProfileEvidence,
  getCapabilitySkipReason,
  readResolvedProfile,
} from '../e2e/resolved-profile'
import { DEPENDENCY_NAMES, parseResolvedProfile } from '../resolved-profile'

type DependencyMode = 'real' | 'mock' | 'disabled'

let tempDir: string
let reportPath: string
const execFileAsync = promisify(execFile)

const dependency = (
  mode: DependencyMode,
  source: 'managed' | 'external' | 'in_process' | 'browser' | null,
) => ({ mode, source })

const createReport = (overrides: Record<string, unknown> = {}) => ({
  reportSchemaVersion: 1,
  generatedAt: '2026-07-16T12:00:00+09:00',
  requestedProfile: 'integration-voice',
  effectiveProfile: 'integration-voice',
  selectionSource: 'DS_PROFILE',
  profile: { schemaVersion: 1, name: 'integration-voice' },
  dependencies: {
    frontend: dependency('real', 'managed'),
    backend: dependency('real', 'managed'),
    ollama: dependency('real', 'managed'),
    voicevox: dependency('real', 'managed'),
    whisper: dependency('real', 'in_process'),
    chroma: dependency('disabled', null),
  },
  capabilities: ['text-chat-real', 'voice-chat-real'],
  derivedEnvironment: {
    OLLAMA_BASE_URL: 'http://localhost:11434',
    VOICEVOX_BASE_URL: 'http://localhost:50021',
    RAG_ENABLED: 'false',
    DS_BACKEND_ORIGIN: 'http://localhost:8000',
  },
  compatibility: { usedEnvironmentVariables: [], warnings: [] },
  ...overrides,
})

const createIntegrationTextReport = () => {
  const report = createReport()
  return createReport({
    effectiveProfile: 'integration-text',
    profile: { schemaVersion: 1, name: 'integration-text' },
    dependencies: {
      ...report.dependencies,
      voicevox: dependency('disabled', null),
      whisper: dependency('disabled', null),
    },
    capabilities: ['text-chat-real'],
  })
}

const writeReport = async (report: unknown) => {
  await writeFile(reportPath, JSON.stringify(report), 'utf-8')
}

describe('resolved profile reader', () => {
  beforeEach(async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'resolved-profile-'))
    reportPath = join(tempDir, 'resolved.json')
    vi.stubEnv(PROFILE_REPORT_ENV, reportPath)
  })

  afterEach(async () => {
    vi.unstubAllEnvs()
    await rm(tempDir, { recursive: true, force: true })
  })

  test('should read a valid V1 report with typed backend mode and capabilities', async () => {
    await writeReport(createReport())

    const report = await readResolvedProfile()

    expect(report.effectiveProfile).toBe('integration-voice')
    expect(report.dependencies.backend.mode).toBe('real')
    expect(report.capabilities).toEqual(['text-chat-real', 'voice-chat-real'])
  })

  test('should fail clearly when DS_PROFILE_REPORT is unset', async () => {
    vi.stubEnv(PROFILE_REPORT_ENV, undefined)

    await expect(readResolvedProfile()).rejects.toThrow('DS_PROFILE_REPORT')
  })

  test('should fail clearly when the report file is missing', async () => {
    await expect(readResolvedProfile()).rejects.toThrow(reportPath)
  })

  test('should fail clearly when the report contains invalid JSON', async () => {
    await writeFile(reportPath, '{', 'utf-8')

    await expect(readResolvedProfile()).rejects.toThrow(/JSON/i)
  })

  test('should reject an unsupported report schema version', async () => {
    await writeReport(createReport({ reportSchemaVersion: 2 }))

    await expect(readResolvedProfile()).rejects.toThrow(/reportSchemaVersion.*2/i)
  })

  test('should keep the shared dependency contract immutable at runtime', () => {
    const dependencyNames = [...DEPENDENCY_NAMES]

    expect(() => (DEPENDENCY_NAMES as unknown as string[]).pop()).toThrow(TypeError)
    const parsed = parseResolvedProfile(createReport())

    expect(DEPENDENCY_NAMES).toEqual(dependencyNames)
    expect(Object.keys(parsed.dependencies)).toEqual(dependencyNames)
  })

  test('should reject a report that omits one of the six dependencies', async () => {
    const report = createReport()
    const { chroma: _, ...incompleteDependencies } = report.dependencies
    await writeReport(createReport({ dependencies: incompleteDependencies }))

    await expect(readResolvedProfile()).rejects.toThrow(/dependencies\.chroma/i)
  })

  test('should reject an invalid dependency mode instead of normalizing it', async () => {
    const report = createReport()
    report.dependencies.backend = dependency('real', 'managed')
    const invalidDependencies = {
      ...report.dependencies,
      backend: { mode: 'auto', source: 'managed' },
    }
    await writeReport(createReport({ dependencies: invalidDependencies }))

    await expect(readResolvedProfile()).rejects.toThrow(/dependencies\.backend\.mode/i)
  })

  test('should reject a non-array capability shape', async () => {
    await writeReport(createReport({ capabilities: { voice: true } }))

    await expect(readResolvedProfile()).rejects.toThrow(/capabilities/i)
  })

  test('should reject an unknown capability value', async () => {
    await writeReport(createReport({
      capabilities: ['text-chat-real', 'voice-chat-real', 'future-capability'],
    }))

    await expect(readResolvedProfile()).rejects.toThrow(/unknown.*future-capability/i)
  })

  test('should accept resolver capabilities without deriving them again', async () => {
    await writeReport(createReport({ capabilities: ['text-chat-real'] }))

    const report = await readResolvedProfile()

    expect(report.capabilities).toEqual(['text-chat-real'])
  })

  test('should return no skip reason when an accepted capability is present', async () => {
    await writeReport(createReport())
    const report = await readResolvedProfile()

    const reason = getCapabilitySkipReason(report, ['mocked-e2e', 'voice-chat-real'])

    expect(reason).toBeNull()
  })

  test('should explain profile and missing alternatives when capability is unavailable', async () => {
    await writeReport(createIntegrationTextReport())
    const report = await readResolvedProfile()

    const reason = getCapabilitySkipReason(report, ['mocked-e2e', 'voice-chat-real'])

    expect(reason).toContain('integration-text')
    expect(reason).toContain('mocked-e2e')
    expect(reason).toContain('voice-chat-real')
    expect(reason).toContain('backend=real/managed')
    expect(reason).toContain('voicevox=disabled/null')
    expect(reason).toContain('whisper=disabled/null')
  })

  test('should attach profile annotation, all dependency modes, and capabilities', async () => {
    await writeReport(createReport())
    const report = await readResolvedProfile()
    const testInfo = {
      annotations: [] as { type: string; description?: string }[],
      attach: vi.fn().mockResolvedValue(undefined),
    }

    await attachProfileEvidence(testInfo, report)

    expect(testInfo.annotations).toEqual([
      { type: 'ds-profile', description: 'integration-voice' },
    ])
    expect(testInfo.attach).toHaveBeenCalledWith(
      'resolved-profile.json',
      {
        body: JSON.stringify({
          profileName: 'integration-voice',
          dependencyModes: {
            frontend: 'real',
            backend: 'real',
            ollama: 'real',
            voicevox: 'real',
            whisper: 'real',
            chroma: 'disabled',
          },
          capabilities: ['text-chat-real', 'voice-chat-real'],
        }, null, 2),
        contentType: 'application/json',
      },
    )
  })

  test('should retain profile evidence when Playwright skips after beforeEach', async () => {
    const runnerDir = await mkdtemp(join(process.cwd(), '.profile-evidence-'))
    const configPath = join(runnerDir, 'playwright.config.mjs')
    const specPath = join(runnerDir, 'skipped.spec.ts')
    await writeFile(configPath, `export default { testDir: ${JSON.stringify(runnerDir)}, reporter: 'json' }`, 'utf-8')
    await writeFile(specPath, `
      import { test } from '@playwright/test'
      import { attachProfileEvidence } from '../e2e/resolved-profile'
      const report = ${JSON.stringify(createReport())}
      test.beforeEach(async ({}, testInfo) => attachProfileEvidence(testInfo, report))
      test('skipped profile', () => test.skip(true, 'capability unavailable'))
    `, 'utf-8')

    try {
      const { stdout } = await execFileAsync(
        join(process.cwd(), 'node_modules', '.bin', 'playwright'),
        ['test', specPath, '--config', configPath],
        { cwd: process.cwd() },
      )
      const result = JSON.parse(stdout) as {
        suites: Array<{ specs: Array<{
          tests: Array<{ annotations: Array<{ type: string }>; results: Array<{
            attachments: Array<{ name: string }>
          }> }>
        }> }>
      }
      const skippedTest = result.suites[0].specs[0].tests[0]

      expect(skippedTest.annotations).toContainEqual(expect.objectContaining({ type: 'ds-profile' }))
      expect(skippedTest.results[0].attachments).toContainEqual(
        expect.objectContaining({ name: 'resolved-profile.json' }),
      )
    } finally {
      await rm(runnerDir, { recursive: true, force: true })
    }
  }, 15_000)

  test('should not allow legacy mode variables to override report content', async () => {
    vi.stubEnv('VOICE_CHAT_E2E_BACKEND', 'mock')
    vi.stubEnv('CHAT_E2E_BACKEND', 'mock')
    await writeReport(createReport())

    const report = await readResolvedProfile()

    expect(report.dependencies.backend.mode).toBe('real')
    expect(report.capabilities).toContain('voice-chat-real')
  })

  test('should read the report produced by the public profile CLI', async () => {
    const env = Object.fromEntries(
      Object.entries(process.env).filter(([name]) => ![
        'VOICE_CHAT_E2E_BACKEND',
        'CHAT_E2E_BACKEND',
        'CHAT_E2E_BACKEND_ORIGIN',
        'VOICE_CHAT_E2E_BACKEND_REPORT',
      ].includes(name)),
    ) as NodeJS.ProcessEnv
    env.DS_PROFILE = 'test-mocked'

    await execFileAsync(
      'python3',
      ['../environments/profile.py', 'resolve', '--report', reportPath],
      { cwd: process.cwd(), env },
    )
    const report = await readResolvedProfile()

    expect(report.effectiveProfile).toBe('test-mocked')
    expect(report.dependencies.backend.mode).toBe('mock')
    expect(report.capabilities).toEqual(['mocked-e2e'])
  })
})
