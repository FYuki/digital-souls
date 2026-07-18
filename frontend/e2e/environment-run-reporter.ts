import type { FullResult, Reporter } from '@playwright/test/reporter'
import { execFile } from 'node:child_process'
import { dirname, join } from 'node:path'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'

const executeFile = promisify(execFile)
const frontendDir = join(dirname(fileURLToPath(import.meta.url)), '..')
const environmentCli = join(frontendDir, '..', 'environments', 'environment_cli.py')

export default class EnvironmentRunReporter implements Reporter {
  async onEnd(result: FullResult): Promise<void> {
    const reportPath = process.env.DS_ENVIRONMENT_RUN_REPORT
    if (reportPath === undefined || reportPath.length === 0) {
      throw new Error('DS_ENVIRONMENT_RUN_REPORT is required')
    }
    const status = result.status === 'passed' ? 'passed' : 'failed'
    const message = `Playwright finished with status ${result.status}`
    await executeFile(
      'python3',
      [
        environmentCli,
        'test-result',
        '--run-report',
        reportPath,
        '--status',
        status,
        '--message',
        message,
      ],
      { cwd: frontendDir },
    )
  }
}
