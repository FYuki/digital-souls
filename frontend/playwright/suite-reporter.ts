import type { FullResult, Reporter } from '@playwright/test/reporter'
import { execFile } from 'node:child_process'
import { readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'

import {
  getSuiteDefinition,
  type SuiteDefinition,
  type SuiteName,
} from './suite-config'

const executeFile = promisify(execFile)
const frontendDir = join(dirname(fileURLToPath(import.meta.url)), '..')
const environmentCli = join(frontendDir, '..', 'environments', 'environment_cli.py')
const PROFILE_REPORT_FILE = 'resolved-profile.json'
const ENVIRONMENT_REPORT_FILE = 'environment-run.json'
const EVIDENCE_FILE = 'evidence.json'

type ReporterOptions = {
  suite: SuiteName
}

type SuiteDefinitionResolver = (suite: SuiteName) => SuiteDefinition

type EnvironmentReport = {
  runId: string
  effectiveProfile: null | { name: string }
  failure: null | { category: string }
}

const parseEnvironmentReport = (value: unknown): EnvironmentReport => {
  if (typeof value !== 'object' || value === null) {
    throw new Error('environment report must be an object')
  }
  const report = value as Record<string, unknown>
  if (typeof report.runId !== 'string' || report.runId.length === 0) {
    throw new Error('environment report runId must be a non-empty string')
  }
  const failure = report.failure
  if (failure !== null && (typeof failure !== 'object' || failure === null)) {
    throw new Error('environment report failure must be an object or null')
  }
  const category = failure === null ? null : (failure as Record<string, unknown>).category
  if (category !== null && typeof category !== 'string') {
    throw new Error('environment report failure category must be a string')
  }
  const effectiveProfile = report.effectiveProfile
  if (effectiveProfile === null) {
    if (category !== 'profile') {
      throw new Error('environment report effectiveProfile can be null only for profile failure')
    }
    return {
      runId: report.runId,
      effectiveProfile: null,
      failure: { category },
    }
  }
  if (typeof effectiveProfile !== 'object') {
    throw new Error('environment report effectiveProfile must be an object or null')
  }
  const profileName = (effectiveProfile as Record<string, unknown>).effectiveProfile
  if (typeof profileName !== 'string' || profileName.length === 0) {
    throw new Error('environment report profile name must be a non-empty string')
  }
  return {
    runId: report.runId,
    effectiveProfile: { name: profileName },
    failure: category === null ? null : { category },
  }
}

export default class SuiteReporter implements Reporter {
  private readonly suite: SuiteDefinition

  constructor(
    options: ReporterOptions,
    resolveSuite: SuiteDefinitionResolver = getSuiteDefinition,
  ) {
    this.suite = resolveSuite(options.suite)
  }

  async onEnd(result: FullResult): Promise<void> {
    const testStatus = result.status === 'passed' ? 'passed' : 'failed'
    const environmentReportPath = join(this.suite.resultDir, ENVIRONMENT_REPORT_FILE)
    await executeFile(
      'python3',
      [
        environmentCli,
        'test-result',
        '--run-report',
        environmentReportPath,
        '--status',
        testStatus,
        '--message',
        `Playwright finished with status ${result.status}`,
      ],
      { cwd: frontendDir },
    )

    const report = parseEnvironmentReport(JSON.parse(
      await readFile(environmentReportPath, 'utf-8'),
    ) as unknown)
    if (report.effectiveProfile !== null && report.effectiveProfile.name !== this.suite.profile) {
      throw new Error(
        `expected profile ${this.suite.profile}, received ${report.effectiveProfile.name}`,
      )
    }

    const evidence = {
      suite: this.suite.suite,
      testLayer: this.suite.testLayer,
      runId: report.runId,
      profile: this.suite.profile,
      testStatus,
      ...(report.effectiveProfile === null ? {} : { profileReport: PROFILE_REPORT_FILE }),
      environmentReport: ENVIRONMENT_REPORT_FILE,
      ...(report.failure === null ? {} : { failureCategory: report.failure.category }),
    }
    await writeFile(
      join(this.suite.resultDir, EVIDENCE_FILE),
      `${JSON.stringify(evidence, null, 2)}\n`,
      'utf-8',
    )
  }
}
