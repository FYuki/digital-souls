import type { FullResult, Reporter } from '@playwright/test/reporter'
import { execFile } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
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

const ENVIRONMENT_FAILURE_CATEGORIES = [
  'profile',
  'preparation',
  'startup',
  'readiness',
  'supervision',
  'test',
  'teardown',
] as const

type EnvironmentFailureCategory = typeof ENVIRONMENT_FAILURE_CATEGORIES[number]

type EnvironmentReport = {
  runId: string
  effectiveProfile: null | { name: string }
  failure: null | { category: EnvironmentFailureCategory }
}

type EvidenceFailureStage =
  | 'test-result'
  | 'environment-report-read'
  | 'environment-report-json'
  | 'environment-report-validation'
  | 'environment-report-association'

const parseFailureCategory = (value: unknown): EnvironmentFailureCategory | null => {
  if (value === null) {
    return null
  }
  if (
    typeof value !== 'string'
    || !ENVIRONMENT_FAILURE_CATEGORIES.includes(value as EnvironmentFailureCategory)
  ) {
    throw new Error(
      `environment report failure category must be one of ${ENVIRONMENT_FAILURE_CATEGORIES.join(', ')}`,
    )
  }
  return value as EnvironmentFailureCategory
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
  const category = parseFailureCategory(
    failure === null ? null : (failure as Record<string, unknown>).category,
  )
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
    await this.recordTestResult(environmentReportPath, testStatus, result.status)
    const reportText = await this.readEnvironmentReport(environmentReportPath, testStatus)
    const reportValue = await this.parseEnvironmentReportJson(reportText, testStatus)
    const report = await this.validateEnvironmentReport(reportValue, testStatus)
    await this.validateReportAssociation(report, testStatus)
    await this.writeEvidence({
      suite: this.suite.suite,
      testLayer: this.suite.testLayer,
      runId: report.runId,
      profile: this.suite.profile,
      testStatus,
      ...(report.effectiveProfile === null ? {} : { profileReport: PROFILE_REPORT_FILE }),
      environmentReport: ENVIRONMENT_REPORT_FILE,
      ...(report.failure === null ? {} : { failureCategory: report.failure.category }),
    })
  }

  private async recordTestResult(
    reportPath: string,
    testStatus: 'passed' | 'failed',
    playwrightStatus: FullResult['status'],
  ): Promise<void> {
    await this.runEvidenceStage('test-result', testStatus, () => executeFile(
      'python3',
      [
        environmentCli,
        'test-result',
        '--run-report',
        reportPath,
        '--status',
        testStatus,
        '--message',
        `Playwright finished with status ${playwrightStatus}`,
      ],
      { cwd: frontendDir },
    ))
  }

  private async readEnvironmentReport(
    reportPath: string,
    testStatus: 'passed' | 'failed',
  ): Promise<string> {
    return this.runEvidenceStage(
      'environment-report-read',
      testStatus,
      () => readFile(reportPath, 'utf-8'),
    )
  }

  private async parseEnvironmentReportJson(
    reportText: string,
    testStatus: 'passed' | 'failed',
  ): Promise<unknown> {
    return this.runEvidenceStage(
      'environment-report-json',
      testStatus,
      () => JSON.parse(reportText) as unknown,
    )
  }

  private async validateEnvironmentReport(
    reportValue: unknown,
    testStatus: 'passed' | 'failed',
  ): Promise<EnvironmentReport> {
    return this.runEvidenceStage(
      'environment-report-validation',
      testStatus,
      () => parseEnvironmentReport(reportValue),
    )
  }

  private async validateReportAssociation(
    report: EnvironmentReport,
    testStatus: 'passed' | 'failed',
  ): Promise<void> {
    await this.runEvidenceStage('environment-report-association', testStatus, async () => {
      if (report.effectiveProfile !== null && report.effectiveProfile.name !== this.suite.profile) {
        throw new Error(
          `expected profile ${this.suite.profile}, received ${report.effectiveProfile.name}`,
        )
      }
    })
  }

  private async runEvidenceStage<T>(
    stage: EvidenceFailureStage,
    testStatus: 'passed' | 'failed',
    operation: () => T | Promise<T>,
  ): Promise<T> {
    try {
      return await operation()
    } catch (error) {
      await this.writeFailedEvidence(stage, testStatus, error)
      throw error
    }
  }

  private async writeFailedEvidence(
    stage: EvidenceFailureStage,
    testStatus: 'passed' | 'failed',
    error: unknown,
  ): Promise<void> {
    await this.writeEvidence({
      suite: this.suite.suite,
      testLayer: this.suite.testLayer,
      profile: this.suite.profile,
      testStatus,
      evidenceStatus: 'failed',
      failureCategory: 'evidence',
      failureStage: stage,
      failureMessage: error instanceof Error ? error.message : String(error),
    })
  }

  private async writeEvidence(evidence: Record<string, unknown>): Promise<void> {
    await mkdir(this.suite.resultDir, { recursive: true })
    await writeFile(
      join(this.suite.resultDir, EVIDENCE_FILE),
      `${JSON.stringify(evidence, null, 2)}\n`,
      'utf-8',
    )
  }
}
