import { existsSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import type { Plugin } from 'vite'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import packageJson from '../package.json'
import { createVadAssetPlugin } from '../vite.vad-assets'
import { VAD_ASSET_ROUTE } from './lib/audio/vad-assets'

const expectedVadAssetNames = [
  'vad.worklet.bundle.min.js',
  'silero_vad_legacy.onnx',
  'ort-wasm-simd-threaded.wasm',
  'ort-wasm-simd-threaded.mjs',
]

const useMiddleware = vi.fn()

const createServer = () => ({
  middlewares: {
    use: useMiddleware,
  },
})

const createResponse = () => ({
  end: vi.fn(),
  setHeader: vi.fn(),
  statusCode: 200,
})

const registerMiddleware = (plugin: Plugin) => {
  if (typeof plugin.configureServer !== 'function') {
    throw new Error('VAD asset plugin does not expose a configureServer hook')
  }

  plugin.configureServer(createServer() as never)
  const middleware = useMiddleware.mock.calls.at(-1)?.[0]
  if (middleware === undefined) {
    throw new Error('VAD asset middleware was not registered')
  }

  return middleware
}

const closeBundle = (plugin: Plugin) => {
  if (typeof plugin.closeBundle !== 'function') {
    throw new Error('VAD asset plugin does not expose a closeBundle hook')
  }

  plugin.closeBundle.call({} as never)
}

describe('VAD asset plugin', () => {
  beforeEach(() => {
    useMiddleware.mockReset()
  })

  test('should serve configured VAD assets from the development middleware', () => {
    const plugin = createVadAssetPlugin()
    const next = vi.fn()
    const response = createResponse()
    const middleware = registerMiddleware(plugin)

    middleware({ url: `${VAD_ASSET_ROUTE}${expectedVadAssetNames[0]}?v=1` }, response, next)

    expect(next).not.toHaveBeenCalled()
    expect(response.setHeader).toHaveBeenCalledWith('Content-Type', 'application/javascript')
    expect(response.end).toHaveBeenCalledWith(expect.any(Buffer))
  })

  test('should return 404 for unknown VAD asset requests', () => {
    const plugin = createVadAssetPlugin()
    const next = vi.fn()
    const response = createResponse()
    const middleware = registerMiddleware(plugin)

    middleware({ url: `${VAD_ASSET_ROUTE}missing.js` }, response, next)

    expect(next).not.toHaveBeenCalled()
    expect(response.statusCode).toBe(404)
    expect(response.end).toHaveBeenCalledWith()
  })

  test('should pass non-VAD requests to the next development middleware', () => {
    const plugin = createVadAssetPlugin()
    const next = vi.fn()
    const response = createResponse()
    const middleware = registerMiddleware(plugin)

    middleware({ url: '/src/App.svelte' }, response, next)

    expect(next).toHaveBeenCalledTimes(1)
    expect(response.end).not.toHaveBeenCalled()
  })

  test('should copy all configured VAD assets into the production build output', () => {
    const plugin = createVadAssetPlugin()

    rmSync(join('dist', VAD_ASSET_ROUTE), { recursive: true, force: true })
    closeBundle(plugin)

    for (const assetName of expectedVadAssetNames) {
      expect(existsSync(join('dist', VAD_ASSET_ROUTE, assetName))).toBe(true)
    }
  })

  test('should declare dependencies for directly copied VAD runtime assets', () => {
    expect(packageJson.dependencies).toHaveProperty('@ricky0123/vad-web')
    expect(packageJson.dependencies).toHaveProperty('onnxruntime-web')
  })
})
