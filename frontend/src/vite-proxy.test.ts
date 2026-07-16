import { execFileSync } from 'node:child_process'
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, test } from 'vitest'

import { API_PROXY_PREFIX, WS_PROXY_PREFIX, createBackendProxy } from '../vite.proxy'


let tempDir: string
let reportPath: string

const writeBackendReport = async (
  backend: { mode: 'real' | 'mock' | 'disabled'; baseUrl?: string },
) => {
  const realBackend = backend.mode === 'real'
  await writeReport({
    reportSchemaVersion: 1,
    generatedAt: '2026-07-16T12:00:00+09:00',
    requestedProfile: 'test-profile',
    effectiveProfile: 'test-profile',
    selectionSource: 'DS_PROFILE',
    profile: { schemaVersion: 1, name: 'test-profile' },
    dependencies: {
      frontend: { mode: 'real', source: 'managed' },
      backend: {
        source: backend.mode === 'mock' ? 'browser' : realBackend ? 'managed' : null,
        ...backend,
      },
      ollama: realBackend
        ? { mode: 'real', source: 'managed', baseUrl: 'http://localhost:11434' }
        : { mode: 'disabled', source: null },
      voicevox: { mode: 'disabled', source: null },
      whisper: { mode: 'disabled', source: null },
      chroma: { mode: 'disabled', source: null },
    },
    capabilities: backend.mode === 'mock'
      ? ['mocked-e2e']
      : realBackend ? ['text-chat-real'] : [],
    derivedEnvironment: {},
    compatibility: { usedEnvironmentVariables: [], warnings: [] },
  })
}

const writeReport = async (report: unknown) => {
  await writeFile(reportPath, JSON.stringify(report), 'utf-8')
}

describe('vite proxy', () => {
  beforeEach(async () => {
    tempDir = await mkdtemp(join(tmpdir(), 'vite-profile-'))
    reportPath = join(tempDir, 'resolved-profile.json')
  })

  afterEach(async () => {
    await rm(tempDir, { recursive: true, force: true })
  })

  test('should forward API and WebSocket traffic to the resolved real backend', async () => {
    const origin = 'http://127.0.0.1:18000'
    await writeBackendReport({ mode: 'real', baseUrl: origin })

    const proxy = createBackendProxy({
      DS_PROFILE_REPORT: reportPath,
      DS_BACKEND_ORIGIN: origin,
    })

    expect(proxy[API_PROXY_PREFIX]).toMatchObject({ target: origin, changeOrigin: true })
    expect(proxy[WS_PROXY_PREFIX]).toMatchObject({ target: origin, changeOrigin: true, ws: true })
    expect(proxy[API_PROXY_PREFIX].rewrite?.('/api/chat')).toBe('/chat')
    expect(proxy[API_PROXY_PREFIX].rewrite?.('/apiary/chat')).toBe('/apiary/chat')
  })

  test('should omit proxy routes for a browser mock backend', async () => {
    await writeBackendReport({ mode: 'mock' })

    const proxy = createBackendProxy({ DS_PROFILE_REPORT: reportPath })

    expect(proxy).toEqual({})
  })

  test('should read the resolver-normalized report after the frontend working directory changes', async () => {
    const resolverWorkingDirectory = join(tempDir, 'resolver-cwd')
    const frontendWorkingDirectory = join(tempDir, 'frontend')
    const relativeReportPath = join('reports', 'run.json')
    await mkdir(resolverWorkingDirectory)
    await mkdir(frontendWorkingDirectory)
    const resolverPath = join(process.cwd(), '..', 'environments', 'profile.py')
    const resolverEnvironment = { ...process.env }
    for (const key of [
      'DS_PROFILE',
      'DS_PROFILE_REPORT',
      'VOICE_CHAT_E2E_BACKEND',
      'CHAT_E2E_BACKEND',
      'CHAT_E2E_BACKEND_ORIGIN',
      'VOICE_CHAT_E2E_BACKEND_REPORT',
    ]) {
      delete resolverEnvironment[key]
    }
    const normalizedReportPath = execFileSync(
      'python3',
      [
        resolverPath,
        'resolve',
        '--report',
        relativeReportPath,
        '--default-profile',
        'test-mocked',
      ],
      { cwd: resolverWorkingDirectory, env: resolverEnvironment, encoding: 'utf-8' },
    ).trim()
    const originalWorkingDirectory = process.cwd()

    process.chdir(frontendWorkingDirectory)
    try {
      expect(normalizedReportPath).toBe(join(resolverWorkingDirectory, relativeReportPath))
      expect(createBackendProxy({ DS_PROFILE_REPORT: normalizedReportPath })).toEqual({})
    } finally {
      process.chdir(originalWorkingDirectory)
    }
  })

  test('should reject a real profile without the resolver-derived backend origin', async () => {
    await writeBackendReport({ mode: 'real', baseUrl: 'http://localhost:8000' })

    expect(() => createBackendProxy({ DS_PROFILE_REPORT: reportPath })).toThrow('DS_BACKEND_ORIGIN')
  })

  test('should reject an origin that differs from the resolved report', async () => {
    await writeBackendReport({ mode: 'real', baseUrl: 'http://localhost:8000' })

    expect(() => createBackendProxy({
      DS_PROFILE_REPORT: reportPath,
      DS_BACKEND_ORIGIN: 'http://localhost:9000',
    })).toThrow(/must match/i)
  })

  test('should reject a report without a schema version', async () => {
    await writeReport({})

    expect(() => createBackendProxy({ DS_PROFILE_REPORT: reportPath })).toThrow(
      /reportSchemaVersion/,
    )
  })

  test('should reject an unsupported report schema version', async () => {
    await writeReport({ reportSchemaVersion: 2 })

    expect(() => createBackendProxy({ DS_PROFILE_REPORT: reportPath })).toThrow(
      /reportSchemaVersion.*2/,
    )
  })

  test('should reject a report that omits one of the six dependencies', async () => {
    await writeReport({
      reportSchemaVersion: 1,
      effectiveProfile: 'incomplete',
      dependencies: {
        frontend: { mode: 'real', source: 'managed' },
        backend: { mode: 'mock', source: 'browser' },
        ollama: { mode: 'disabled', source: null },
        voicevox: { mode: 'disabled', source: null },
        whisper: { mode: 'disabled', source: null },
      },
      capabilities: ['mocked-e2e'],
    })

    expect(() => createBackendProxy({ DS_PROFILE_REPORT: reportPath })).toThrow(
      /dependencies\.chroma/,
    )
  })

  test('should reject a dependency without a source', async () => {
    await writeReport({
      reportSchemaVersion: 1,
      effectiveProfile: 'invalid-source',
      dependencies: {
        frontend: { mode: 'real', source: 'managed' },
        backend: { mode: 'mock' },
        ollama: { mode: 'disabled', source: null },
        voicevox: { mode: 'disabled', source: null },
        whisper: { mode: 'disabled', source: null },
        chroma: { mode: 'disabled', source: null },
      },
      capabilities: ['mocked-e2e'],
    })

    expect(() => createBackendProxy({ DS_PROFILE_REPORT: reportPath })).toThrow(
      /dependencies\.backend\.source/,
    )
  })
})
