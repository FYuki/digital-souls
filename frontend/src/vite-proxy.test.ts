import { describe, expect, test } from 'vitest'

import { API_PROXY_PREFIX, apiProxyConfig, BACKEND_ORIGIN } from '../vite.proxy'

describe('vite proxy', () => {
  test('should forward /api requests to the backend after removing the /api prefix', () => {
    const rewrittenPath = apiProxyConfig.rewrite('/api/chat')

    expect(API_PROXY_PREFIX).toBe('/api')
    expect(apiProxyConfig).toMatchObject({
      target: BACKEND_ORIGIN,
      changeOrigin: true,
    })
    expect(rewrittenPath).toBe('/chat')
  })

  test('should only remove the exact /api prefix boundary', () => {
    expect(apiProxyConfig.rewrite('/api')).toBe('')
    expect(apiProxyConfig.rewrite('/apiary/chat')).toBe('/apiary/chat')
  })
})
