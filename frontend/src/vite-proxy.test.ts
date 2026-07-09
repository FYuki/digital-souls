import { randomUUID } from 'node:crypto'
import { pathToFileURL } from 'node:url'
import { join } from 'node:path'

import { afterEach, describe, expect, test, vi } from 'vitest'

const viteProxySourcePath = join(process.cwd(), 'vite.proxy.ts')

const loadViteProxy = async () => {
  vi.resetModules()
  const configUrl = `${pathToFileURL(viteProxySourcePath).href}?test=${randomUUID()}`
  return import(configUrl)
}

describe('vite proxy', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.resetModules()
  })

  test('should forward /api requests to the backend after removing the /api prefix', async () => {
    const { API_PROXY_PREFIX, apiProxyConfig } = await loadViteProxy()
    const rewrittenPath = apiProxyConfig.rewrite('/api/chat')

    expect(API_PROXY_PREFIX).toBe('/api')
    expect(apiProxyConfig).toMatchObject({
      target: 'http://localhost:8000',
      changeOrigin: true,
    })
    expect(rewrittenPath).toBe('/chat')
  })

  test('should only remove the exact /api prefix boundary', async () => {
    const { apiProxyConfig } = await loadViteProxy()

    expect(apiProxyConfig.rewrite('/api')).toBe('')
    expect(apiProxyConfig.rewrite('/apiary/chat')).toBe('/apiary/chat')
  })

  test('should forward /ws WebSocket connections to the backend', async () => {
    const { WS_PROXY_PREFIX, wsProxyConfig } = await loadViteProxy()

    expect(WS_PROXY_PREFIX).toBe('/ws')
    expect(wsProxyConfig).toMatchObject({
      target: 'http://localhost:8000',
      changeOrigin: true,
      ws: true,
    })
  })

  test('should keep shared proxy targets on the default backend when chat real mode is configured', async () => {
    vi.stubEnv('CHAT_E2E_BACKEND', 'real')
    vi.stubEnv('CHAT_E2E_BACKEND_ORIGIN', 'http://127.0.0.1:18000')

    const { apiProxyConfig, wsProxyConfig } = await loadViteProxy()

    expect(apiProxyConfig.target).toBe('http://localhost:8000')
    expect(wsProxyConfig.target).toBe('http://localhost:8000')
  })

  test('should keep the default backend when chat mock mode has an origin env', async () => {
    vi.stubEnv('CHAT_E2E_BACKEND', 'mock')
    vi.stubEnv('CHAT_E2E_BACKEND_ORIGIN', 'http://127.0.0.1:18000')

    const { apiProxyConfig, wsProxyConfig } = await loadViteProxy()

    expect(apiProxyConfig.target).toBe('http://localhost:8000')
    expect(wsProxyConfig.target).toBe('http://localhost:8000')
  })
})
