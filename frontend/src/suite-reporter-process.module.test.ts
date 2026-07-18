import { execFile } from 'node:child_process'
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

import { describe, expect, test } from 'vitest'

type ProcessResult = {
  error: Error | null
  stderr: string
}

type ProcessRunner = {
  runnerDir: string
  resultDir: string
  configPath: string
}

const executePlaywright = (configPath: string): Promise<ProcessResult> => new Promise((resolve) => {
  execFile(
    join(process.cwd(), 'node_modules', '.bin', 'playwright'),
    ['test', 'passing.spec.ts', '--config', configPath],
    { cwd: process.cwd() },
    (error, _stdout, stderr) => resolve({ error, stderr }),
  )
})

const prepareRunner = async (): Promise<ProcessRunner> => {
  const runnerDir = await mkdtemp(join(process.cwd(), '.suite-reporter-process-'))
  const workspaceRoot = join(runnerDir, 'workspace')
  const workspaceFrontend = join(workspaceRoot, 'frontend')
  const resultDir = join(workspaceFrontend, 'test-results', 'integration-text')
  await mkdir(workspaceFrontend, { recursive: true })
  await Promise.all([
    cp(join(process.cwd(), 'playwright'), join(workspaceFrontend, 'playwright'), {
      recursive: true,
    }),
    cp(join(process.cwd(), '..', 'environments'), join(workspaceRoot, 'environments'), {
      recursive: true,
    }),
    cp(join(process.cwd(), '..', 'backend', 'app'), join(workspaceRoot, 'backend', 'app'), {
      recursive: true,
    }),
    cp(
      join(process.cwd(), 'resolved-profile.ts'),
      join(workspaceFrontend, 'resolved-profile.ts'),
    ),
  ])
  await mkdir(resultDir, { recursive: true })
  const configPath = join(runnerDir, 'playwright.config.mjs')
  const reporterPath = join(workspaceFrontend, 'playwright', 'suite-reporter-entrypoint.ts')
  await writeFile(
    configPath,
    `export default {
      testDir: ${JSON.stringify(runnerDir)},
      workers: 1,
      reporter: [[${JSON.stringify(reporterPath)}, { suite: 'integration-text' }]],
    }`,
    'utf-8',
  )
  await writeFile(
    join(runnerDir, 'passing.spec.ts'),
    `import { test, expect } from '@playwright/test'
     test('passes before reporter finalization', () => expect(true).toBe(true))`,
    'utf-8',
  )
  return { runnerDir, resultDir, configPath }
}

const service = (mode: 'real' | 'disabled', source: 'managed' | null) => ({
  mode,
  source,
  state: mode === 'disabled' ? 'disabled' : 'pending',
  owned: false,
  processIdentity: null,
  containerIdentity: null,
  readiness: null,
})

const validEnvironmentReport = () => {
  const dependencies = {
    frontend: { mode: 'real', source: 'managed' },
    backend: { mode: 'real', source: 'managed' },
    ollama: { mode: 'real', source: 'managed' },
    voicevox: { mode: 'disabled', source: null },
    whisper: { mode: 'disabled', source: null },
    chroma: { mode: 'disabled', source: null },
  }
  return {
    schemaVersion: 1,
    runId: 'process-run-integration-text',
    startedAt: '2026-07-18T00:00:00+00:00',
    readyAt: null,
    endedAt: null,
    resolvedProfilePath: '/runtime/resolved-profile.json',
    orchestratorIdentity: {
      pid: 2_147_483_647,
      pgid: 2_147_483_647,
      sessionId: 2_147_483_647,
      startTime: 1,
    },
    effectiveProfile: { effectiveProfile: 'integration-text', dependencies },
    phase: 'verify',
    status: 'running',
    startSequence: [],
    services: {
      frontend: service('real', 'managed'),
      backend: service('real', 'managed'),
      ollama: service('real', 'managed'),
      voicevox: service('disabled', null),
      whisper: service('disabled', null),
      chroma: service('disabled', null),
    },
    testResult: null,
    failure: null,
    teardown: { status: 'pending', results: [] },
  }
}

describe('Playwright suite reporter process boundary', () => {
  test('should exit non-zero and retain failed evidence when report association fails', async () => {
    const runner = await prepareRunner()

    try {
      await writeFile(join(runner.resultDir, 'environment-run.json'), '{', 'utf-8')

      const execution = await executePlaywright(runner.configPath)
      const evidence = JSON.parse(
        await readFile(join(runner.resultDir, 'evidence.json'), 'utf-8'),
      ) as Record<string, unknown>

      expect(execution.error).not.toBeNull()
      expect(execution.stderr).toMatch(/environment|report|JSON/i)
      expect(evidence).toEqual(expect.objectContaining({
        suite: 'integration-text',
        testStatus: 'passed',
        evidenceStatus: 'failed',
        failureCategory: 'evidence',
        failureStage: 'test-result',
      }))
    } finally {
      await rm(runner.runnerDir, { recursive: true, force: true })
    }
  }, 20_000)

  test('should exit zero and write linked evidence when reporter finalization succeeds', async () => {
    const runner = await prepareRunner()

    try {
      await Promise.all([
        writeFile(
          join(runner.resultDir, 'environment-run.json'),
          JSON.stringify(validEnvironmentReport()),
          'utf-8',
        ),
        writeFile(join(runner.resultDir, 'resolved-profile.json'), '{}', 'utf-8'),
      ])

      const execution = await executePlaywright(runner.configPath)
      const evidence = JSON.parse(
        await readFile(join(runner.resultDir, 'evidence.json'), 'utf-8'),
      ) as Record<string, unknown>

      expect(execution.error).toBeNull()
      expect(execution.stderr).toBe('')
      expect(evidence).toEqual({
        suite: 'integration-text',
        testLayer: 'integration',
        runId: 'process-run-integration-text',
        profile: 'integration-text',
        testStatus: 'passed',
        profileReport: 'resolved-profile.json',
        environmentReport: 'environment-run.json',
      })
    } finally {
      await rm(runner.runnerDir, { recursive: true, force: true })
    }
  }, 20_000)
})
