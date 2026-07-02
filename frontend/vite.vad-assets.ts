import { copyFileSync, existsSync, mkdirSync, readFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import type { Plugin } from 'vite'

import { VAD_ASSET_ROUTE } from './src/lib/audio/vad-assets'

const vadAssetFiles = [
  'node_modules/@ricky0123/vad-web/dist/vad.worklet.bundle.min.js',
  'node_modules/@ricky0123/vad-web/dist/silero_vad_legacy.onnx',
  'node_modules/onnxruntime-web/dist/ort-wasm-simd-threaded.wasm',
  'node_modules/onnxruntime-web/dist/ort-wasm-simd-threaded.mjs',
]

const getVadAssetContentType = (fileName: string): string => {
  if (fileName.endsWith('.js') || fileName.endsWith('.mjs')) {
    return 'application/javascript'
  }

  if (fileName.endsWith('.wasm')) {
    return 'application/wasm'
  }

  return 'application/octet-stream'
}

export const createVadAssetPlugin = (): Plugin => ({
  name: 'vad-static-assets',
  configureServer(server) {
    server.middlewares.use((request, response, next) => {
      const requestUrl = request.url?.split('?')[0]

      if (requestUrl === undefined || !requestUrl.startsWith(VAD_ASSET_ROUTE)) {
        next()
        return
      }

      const assetPath = vadAssetFiles.find((filePath) => {
        return basename(filePath) === requestUrl.slice(VAD_ASSET_ROUTE.length)
      })

      if (assetPath === undefined) {
        response.statusCode = 404
        response.end()
        return
      }

      response.setHeader('Content-Type', getVadAssetContentType(assetPath))
      response.end(readFileSync(assetPath))
    })
  },
  closeBundle() {
    const outputDirectory = join('dist', VAD_ASSET_ROUTE)
    mkdirSync(outputDirectory, { recursive: true })

    for (const assetFile of vadAssetFiles) {
      if (!existsSync(assetFile)) {
        throw new Error(`VAD asset is missing: ${assetFile}`)
      }

      copyFileSync(assetFile, join(outputDirectory, basename(assetFile)))
    }
  },
})
