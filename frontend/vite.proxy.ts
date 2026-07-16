import { readFileSync } from 'node:fs'

import type { ProxyOptions } from 'vite'

import {
  BACKEND_ORIGIN_ENV,
  PROFILE_REPORT_ENV,
  parseResolvedProfile,
} from './resolved-profile'

export const API_PROXY_PREFIX = '/api'
export const WS_PROXY_PREFIX = '/ws'
const readResolvedProfile = (reportPath: string) => {
  let parsed: unknown
  try {
    parsed = JSON.parse(readFileSync(reportPath, 'utf-8')) as unknown
  } catch (error) {
    throw new Error(`Vite failed to read resolved profile report: ${reportPath}`, { cause: error })
  }
  return parseResolvedProfile(parsed)
}

const createRealBackendProxy = (origin: string): Record<string, ProxyOptions> => ({
  [API_PROXY_PREFIX]: {
    target: origin,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api(?=\/|$)/, ''),
  },
  [WS_PROXY_PREFIX]: {
    target: origin,
    changeOrigin: true,
    ws: true,
  },
})

export const createBackendProxy = (env: NodeJS.ProcessEnv): Record<string, ProxyOptions> => {
  const reportPath = env[PROFILE_REPORT_ENV]
  if (reportPath === undefined || reportPath.length === 0) {
    throw new Error(`${PROFILE_REPORT_ENV} must identify the resolved profile report`)
  }
  const backend = readResolvedProfile(reportPath).dependencies.backend
  if (backend.mode === 'mock') {
    return {}
  }
  if (backend.mode === 'disabled') {
    throw new Error('resolved profile report disables the backend required by Vite')
  }
  const origin = env[BACKEND_ORIGIN_ENV]
  if (origin === undefined || origin.length === 0) {
    throw new Error(`${BACKEND_ORIGIN_ENV} is required for a real backend profile`)
  }
  if (backend.baseUrl !== origin) {
    throw new Error(`${BACKEND_ORIGIN_ENV} must match resolved dependencies.backend.baseUrl`)
  }
  return createRealBackendProxy(origin)
}
