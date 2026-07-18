import type { FullResult, Reporter } from '@playwright/test/reporter'

import SuiteReporter from './suite-reporter'

type ReporterOptions = ConstructorParameters<typeof SuiteReporter>[0]

export default class SuiteReporterEntrypoint implements Reporter {
  private readonly reporter: SuiteReporter

  constructor(options: ReporterOptions) {
    this.reporter = new SuiteReporter(options)
  }

  async onEnd(result: FullResult): Promise<{ status: 'failed' } | undefined> {
    try {
      await this.reporter.onEnd(result)
      return undefined
    } catch (error) {
      console.error(error)
      return { status: 'failed' }
    }
  }
}
