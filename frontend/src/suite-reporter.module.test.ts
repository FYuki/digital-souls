import type { FullResult } from '@playwright/test/reporter'
import { execFile } from 'node:child_process'
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import SuiteReporter from '../playwright/suite-reporter'
import {
  getSuiteDefinition,
  type SuiteDefinition,
  type SuiteName,
} from '../playwright/suite-config'

vi.mock('node:child_process', () => {
  const mockedExecFile = vi.fn(
    (
      _file: string,
      _arguments: string[],
      _options: object,
      callback: (error: Error | null, result: { stdout: string; stderr: string }) => void,
    ) => callback(null, { stdout: '', stderr: '' }),
  )
  return { default: { execFile: mockedExecFile }, execFile: mockedExecFile }
})

let outputDir: string

const environmentReport = (overrides: Record<string, unknown> = {}) => ({
  schemaVersion: 1,
  runId: 'run-integration-text-123',
  effectiveProfile: {
    effectiveProfile: 'integration-text',
    profile: { name: 'integration-text' },
  },
  status: 'completed',
  testResult: { status: 'passed' },
  failure: null,
  ...overrides,
})

const resolveSuiteForTest = (suite: SuiteName): SuiteDefinition => Object.freeze({
  ...getSuiteDefinition(suite),
  resultDir: outputDir,
})

const createReporter = () => new SuiteReporter(
  { suite: 'integration-text' },
  resolveSuiteForTest,
)

const readEvidence = async () => JSON.parse(
  await readFile(join(outputDir, 'evidence.json'), 'utf-8'),
) as Record<string, unknown>

describe('Playwright suite evidence reporter', () => {
  beforeEach(async () => {
    outputDir = await mkdtemp(join(tmpdir(), 'suite-evidence-'))
  })

  afterEach(async () => {
    vi.clearAllMocks()
    await rm(outputDir, { recursive: true, force: true })
  })

  test('writes passed integration evidence linked to the same environment run', async () => {
    await Promise.all([
      writeFile(
        join(outputDir, 'environment-run.json'),
        JSON.stringify(environmentReport()),
        'utf-8',
      ),
      writeFile(join(outputDir, 'resolved-profile.json'), '{}', 'utf-8'),
      writeFile(join(outputDir, 'playwright-results.json'), '{}', 'utf-8'),
    ])

    await createReporter().onEnd({ status: 'passed' } as FullResult)

    expect(await readEvidence()).toEqual({
      suite: 'integration-text',
      testLayer: 'integration',
      runId: 'run-integration-text-123',
      profile: 'integration-text',
      testStatus: 'passed',
      profileReport: 'resolved-profile.json',
      environmentReport: 'environment-run.json',
    })
    expect((await readdir(outputDir)).sort()).toEqual([
      'environment-run.json',
      'evidence.json',
      'playwright-results.json',
      'resolved-profile.json',
    ])
    expect(execFile).toHaveBeenCalledWith(
      'python3',
      expect.arrayContaining([
        'test-result',
        '--run-report',
        join(outputDir, 'environment-run.json'),
        '--status',
        'passed',
      ]),
      expect.any(Object),
      expect.any(Function),
    )
  })

  test.each([
    { category: 'profile', effectiveProfile: null },
    {
      category: 'readiness',
      effectiveProfile: {
        effectiveProfile: 'integration-text',
        profile: { name: 'integration-text' },
      },
    },
  ])('preserves an existing $category environment failure category', async ({
    category,
    effectiveProfile,
  }) => {
    await writeFile(
      join(outputDir, 'environment-run.json'),
      JSON.stringify(environmentReport({
        effectiveProfile,
        status: 'failed',
        testResult: { status: 'failed' },
        failure: { category, message: `${category} failed` },
      })),
      'utf-8',
    )

    await createReporter().onEnd({ status: 'failed' } as FullResult)

    expect(await readEvidence()).toEqual(expect.objectContaining({
      suite: 'integration-text',
      testStatus: 'failed',
      failureCategory: category,
    }))
    if (category === 'profile') {
      expect(await readEvidence()).not.toHaveProperty('profileReport')
    }
  })

  test('does not link stale profile evidence to a later profile-resolution failure', async () => {
    await Promise.all([
      writeFile(join(outputDir, 'resolved-profile.json'), '{"runId":"stale-run"}', 'utf-8'),
      writeFile(
        join(outputDir, 'environment-run.json'),
        JSON.stringify(environmentReport({
          effectiveProfile: null,
          status: 'failed',
          testResult: { status: 'failed' },
          failure: { category: 'profile', message: 'profile failed' },
        })),
        'utf-8',
      ),
    ])

    await createReporter().onEnd({ status: 'failed' } as FullResult)

    expect(await readEvidence()).not.toHaveProperty('profileReport')
  })

  test('rejects a null effective profile without a profile failure', async () => {
    await writeFile(
      join(outputDir, 'environment-run.json'),
      JSON.stringify(environmentReport({ effectiveProfile: null })),
      'utf-8',
    )

    await expect(createReporter().onEnd({ status: 'failed' } as FullResult))
      .rejects.toThrow(/effectiveProfile.*profile failure/i)
  })

  test('classifies a Playwright failure as test failure when the environment has no failure', async () => {
    await writeFile(
      join(outputDir, 'environment-run.json'),
      JSON.stringify(environmentReport({
        status: 'failed',
        testResult: { status: 'failed' },
        failure: { category: 'test', message: 'Playwright failed' },
      })),
      'utf-8',
    )

    await createReporter().onEnd({ status: 'failed' } as FullResult)

    expect(await readEvidence()).toEqual(expect.objectContaining({ failureCategory: 'test' }))
  })

  test('fails instead of inventing a runId when the environment report is malformed', async () => {
    await writeFile(
      join(outputDir, 'environment-run.json'),
      JSON.stringify(environmentReport({ runId: '' })),
      'utf-8',
    )

    await expect(createReporter().onEnd({ status: 'failed' } as FullResult))
      .rejects.toThrow(/runId/i)
  })

  test('rejects an environment report from a different profile', async () => {
    await writeFile(
      join(outputDir, 'environment-run.json'),
      JSON.stringify(environmentReport({
        effectiveProfile: {
          effectiveProfile: 'integration-voice',
          profile: { name: 'integration-voice' },
        },
      })),
      'utf-8',
    )

    await expect(createReporter().onEnd({ status: 'passed' } as FullResult))
      .rejects.toThrow(/integration-text.*integration-voice|integration-voice.*integration-text/i)
  })

  test('rejects an unknown suite before writing evidence', () => {
    expect(() => new SuiteReporter({ suite: 'unknown-suite' as SuiteName }))
      .toThrow(/unknown Playwright suite/i)
  })
})
