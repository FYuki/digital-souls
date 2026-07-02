import { describe, expect, test } from 'vitest'

import {
  API_PROXY_PREFIX,
  WS_PROXY_PREFIX,
  apiProxyConfig,
  wsProxyConfig,
} from '../vite.proxy'

describe('vite proxy', () => {
  test('should forward /api requests to the backend after removing the /api prefix', () => {
    const rewrittenPath = apiProxyConfig.rewrite('/api/chat')

    expect(API_PROXY_PREFIX).toBe('/api')
    expect(apiProxyConfig).toMatchObject({
      target: 'http://localhost:8000',
      changeOrigin: true,
    })
    expect(rewrittenPath).toBe('/chat')
  })

  test('should only remove the exact /api prefix boundary', () => {
    expect(apiProxyConfig.rewrite('/api')).toBe('')
    expect(apiProxyConfig.rewrite('/apiary/chat')).toBe('/apiary/chat')
  })

  test('should forward /ws WebSocket connections to the backend', () => {
    expect(WS_PROXY_PREFIX).toBe('/ws')
    expect(wsProxyConfig).toMatchObject({
      target: 'http://localhost:8000',
      changeOrigin: true,
      ws: true,
    })
  })
})
