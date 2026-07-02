import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { BACKEND_REPORT_ENV, resolveVoiceChatBackend } from '../e2e/voice-chat-backend'

let tempDir: string
let reportPath: string

const writeBackendReport = async (content: unknown) => {
  await writeFile(reportPath, JSON.stringify(content), 'utf-8')
}

describe('voice chat backend startup report resolver', () => {
  beforeEach(async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'voice-chat-backend-'))
    reportPath = join(tempDir, 'backend-report.json')
    vi.stubEnv(BACKEND_REPORT_ENV, reportPath)
  })

  afterEach(async () => {
    vi.unstubAllEnvs()
    await rm(tempDir, { recursive: true, force: true })
  })

  test('should read the backend mode selected by the startup boundary', async () => {
    await writeBackendReport({
      mode: 'real',
      reasons: ['backend, Ollama, and VOICEVOX startup checks passed'],
    })

    await expect(resolveVoiceChatBackend()).resolves.toEqual({
      mode: 'real',
      reasons: ['backend, Ollama, and VOICEVOX startup checks passed'],
    })
  })

  test('should read mock mode without performing duplicate health checks', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
    await writeBackendReport({
      mode: 'mock',
      reasons: ['VOICE_CHAT_E2E_BACKEND=mock requested'],
    })

    await expect(resolveVoiceChatBackend()).resolves.toEqual({
      mode: 'mock',
      reasons: ['VOICE_CHAT_E2E_BACKEND=mock requested'],
    })
    expect(fetch).not.toHaveBeenCalled()
  })

  test('should fail fast when the startup report shape is invalid', async () => {
    await writeBackendReport({ mode: 'auto', reasons: [] })

    await expect(resolveVoiceChatBackend()).rejects.toThrow(
      `voice chat backend report shape is invalid: ${reportPath}`,
    )
  })
})
