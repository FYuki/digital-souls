import type { FullResult } from '@playwright/test/reporter'
import { execFile } from 'node:child_process'

import { afterEach, describe, expect, test, vi } from 'vitest'

import EnvironmentRunReporter from '../e2e/environment-run-reporter'

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

describe('environment run Playwright reporter', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.clearAllMocks()
  })

  test('records a non-passing Playwright result through the environment CLI', async () => {
    vi.stubEnv('DS_ENVIRONMENT_RUN_REPORT', '/runtime/environment-run.json')
    const reporter = new EnvironmentRunReporter()

    await reporter.onEnd({ status: 'failed' } as FullResult)

    expect(execFile).toHaveBeenCalledWith(
      'python3',
      expect.arrayContaining([
        'test-result',
        '--run-report',
        '/runtime/environment-run.json',
        '--status',
        'failed',
      ]),
      expect.any(Object),
      expect.any(Function),
    )
  })
})
