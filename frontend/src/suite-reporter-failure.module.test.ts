import type { FullResult } from '@playwright/test/reporter'
import { execFile } from 'node:child_process'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
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

type ExecFileImplementation = (
  file: string,
  arguments_: string[],
  options: object,
  callback: (error: Error | null, result: { stdout: string; stderr: string }) => void,
) => void

const failNextExecution = (error: Error) => {
  const mockedExecFile = execFile as unknown as {
    mockImplementationOnce: (implementation: ExecFileImplementation) => void
  }
  mockedExecFile.mockImplementationOnce((
    _file,
    _arguments,
    _options,
    callback,
  ) => callback(error, { stdout: '', stderr: '' }))
}

const readEvidence = async () => JSON.parse(
  await readFile(join(outputDir, 'evidence.json'), 'utf-8'),
) as Record<string, unknown>

const expectFailedEvidence = async (stage: string) => {
  const evidence = await readEvidence()
  expect(evidence).toEqual(expect.objectContaining({
    suite: 'integration-text',
    testLayer: 'integration',
    profile: 'integration-text',
    testStatus: 'passed',
    evidenceStatus: 'failed',
    failureCategory: 'evidence',
    failureStage: stage,
    failureMessage: expect.any(String),
  }))
  expect(evidence).not.toHaveProperty('runId')
  expect(evidence).not.toHaveProperty('profileReport')
  expect(evidence).not.toHaveProperty('environmentReport')
  expect(evidence.failureMessage).toMatch(/\S/)
}

describe('Playwright suite evidence reporter failures', () => {
  beforeEach(async () => {
    outputDir = await mkdtemp(join(tmpdir(), 'suite-evidence-failure-'))
  })

  afterEach(async () => {
    vi.clearAllMocks()
    await rm(outputDir, { recursive: true, force: true })
  })

  test('should replace stale success evidence and rethrow the original test-result error', async () => {
    const testResultError = new Error('environment CLI failed')
    await writeFile(
      join(outputDir, 'evidence.json'),
      JSON.stringify({ evidenceStatus: 'passed', runId: 'stale-run' }),
      'utf-8',
    )
    failNextExecution(testResultError)

    const result = createReporter().onEnd({ status: 'passed' } as FullResult)

    await expect(result).rejects.toBe(testResultError)
    await expectFailedEvidence('test-result')
    expect(await readEvidence()).toEqual(expect.objectContaining({
      failureMessage: 'environment CLI failed',
    }))
  })

  test('should create the result directory before writing a test-result failure', async () => {
    const testResultError = new Error('environment CLI could not start')
    await rm(outputDir, { recursive: true })
    failNextExecution(testResultError)

    await expect(createReporter().onEnd({ status: 'passed' } as FullResult))
      .rejects.toBe(testResultError)

    await expectFailedEvidence('test-result')
  })

  test('should preserve a failed Playwright result in failed evidence', async () => {
    const testResultError = new Error('environment CLI failed after tests')
    failNextExecution(testResultError)

    await expect(createReporter().onEnd({ status: 'failed' } as FullResult))
      .rejects.toBe(testResultError)

    expect(await readEvidence()).toEqual(expect.objectContaining({
      testStatus: 'failed',
      evidenceStatus: 'failed',
      failureStage: 'test-result',
    }))
  })

  test('should write failed evidence when the environment report is missing', async () => {
    const result = createReporter().onEnd({ status: 'passed' } as FullResult)

    await expect(result).rejects.toThrow(/ENOENT|no such file/i)
    await expectFailedEvidence('environment-report-read')
  })

  test('should write failed evidence when the environment report cannot be read', async () => {
    await mkdir(join(outputDir, 'environment-run.json'))

    const result = createReporter().onEnd({ status: 'passed' } as FullResult)

    await expect(result).rejects.toThrow()
    await expectFailedEvidence('environment-report-read')
  })

  test('should classify malformed JSON separately from report validation', async () => {
    await writeFile(join(outputDir, 'environment-run.json'), '{', 'utf-8')

    const result = createReporter().onEnd({ status: 'passed' } as FullResult)

    await expect(result).rejects.toThrow(/JSON|position|property name/i)
    await expectFailedEvidence('environment-report-json')
  })

  test('should classify an environment report contract violation as validation failure', async () => {
    await writeFile(
      join(outputDir, 'environment-run.json'),
      JSON.stringify(environmentReport({ runId: '' })),
      'utf-8',
    )

    const result = createReporter().onEnd({ status: 'passed' } as FullResult)

    await expect(result).rejects.toThrow(/runId/i)
    await expectFailedEvidence('environment-report-validation')
  })

  test.each(['', 'evidence', 'unknown-category'])(
    'should reject the contract-external failure category %j as validation failure',
    async (category) => {
      await writeFile(
        join(outputDir, 'environment-run.json'),
        JSON.stringify(environmentReport({
          status: 'failed',
          failure: { category, message: 'invalid category' },
        })),
        'utf-8',
      )

      const result = createReporter().onEnd({ status: 'passed' } as FullResult)

      await expect(result).rejects.toThrow(/failure category.*one of/i)
      await expectFailedEvidence('environment-report-validation')
    },
  )

  test('should classify a different effective profile as association failure', async () => {
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

    const result = createReporter().onEnd({ status: 'passed' } as FullResult)

    await expect(result).rejects.toThrow(
      /integration-text.*integration-voice|integration-voice.*integration-text/i,
    )
    await expectFailedEvidence('environment-report-association')
  })
})
