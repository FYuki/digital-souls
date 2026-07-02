import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

export const BACKEND_REPORT_ENV = 'VOICE_CHAT_E2E_BACKEND_REPORT'

const DEFAULT_BACKEND_REPORT_PATH = join(process.cwd(), 'test-results/voice-chat-backend.json')

export type VoiceChatBackend = {
  mode: 'real' | 'mock'
  reasons: string[]
}

const resolveBackendReportPath = (): string => {
  const reportPath = process.env[BACKEND_REPORT_ENV]
  if (reportPath === undefined || reportPath.length === 0) {
    return DEFAULT_BACKEND_REPORT_PATH
  }

  return reportPath
}

const isVoiceChatBackend = (value: unknown): value is VoiceChatBackend => {
  if (typeof value !== 'object' || value === null) {
    return false
  }

  const record = value as Record<string, unknown>
  return (
    (record.mode === 'real' || record.mode === 'mock') &&
    Array.isArray(record.reasons) &&
    record.reasons.every((reason) => typeof reason === 'string')
  )
}

export const resolveVoiceChatBackend = async (): Promise<VoiceChatBackend> => {
  const reportPath = resolveBackendReportPath()
  const report = JSON.parse(await readFile(reportPath, 'utf-8')) as unknown

  if (!isVoiceChatBackend(report)) {
    throw new Error(`voice chat backend report shape is invalid: ${reportPath}`)
  }

  return report
}
