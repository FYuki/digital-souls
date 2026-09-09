// 生記録とserver clock境界を照合したtask内入力から、本文・IDを含まない数値診断を新規保存する。
import {readFile, writeFile} from 'node:fs/promises'
import {replayStaleWindow, type StaleWindowInput} from '../playwright/replay-stale-window'

try {
  if (process.argv.length !== 4) throw new Error('invalid_arguments')
  const input = JSON.parse(await readFile(process.argv[2], 'utf8')) as {measurementRevision: string; trials: StaleWindowInput[]}
  if (!/^[a-f0-9]{40}$/.test(input.measurementRevision) || !Array.isArray(input.trials)
    || input.trials.length < 1 || input.trials.length > 100) throw new Error('invalid_input')
  const report = {measurementRevision: input.measurementRevision, scope: 'server_cancel_window_diagnostic',
    trials: input.trials.map(replayStaleWindow), fullStaleAcceptanceVerified: false}
  await writeFile(process.argv[3], JSON.stringify(report, null, 2) + '\n', {flag: 'wx', mode: 0o600})
  process.stdout.write(JSON.stringify({measured: report.trials.length,
    completeOutputWindows: report.trials.filter(t => t.audio.complete).length,
    outputWindowsWithPossibleStale: report.trials.filter(t => (t.audio.audit?.nonzeroSamplesAfterCancelUpper ?? 0) > 0).length,
    fullStaleAcceptanceVerified: false}) + '\n')
} catch {process.stderr.write('stale_window_diagnostic_failed\n'); process.exitCode = 2}
