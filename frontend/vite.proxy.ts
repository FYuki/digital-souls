import type { ProxyOptions } from 'vite'

export const API_PROXY_PREFIX = '/api'
export const BACKEND_ORIGIN = 'http://localhost:8000'

export const apiProxyConfig = {
  target: BACKEND_ORIGIN,
  changeOrigin: true,
  rewrite: (path: string) => path.replace(/^\/api(?=\/|$)/, ''),
} satisfies ProxyOptions
