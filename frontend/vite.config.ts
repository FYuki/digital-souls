import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte'
import { defineConfig } from 'vite'

import { createVadAssetPlugin } from './vite.vad-assets'
import {
  createBackendProxy,
} from './vite.proxy'

export default defineConfig(({ command, mode, isPreview }) => ({
  plugins: [svelte({ preprocess: vitePreprocess() }), createVadAssetPlugin()],
  resolve: {
    conditions: ['browser'],
  },
  server: {
    proxy: command === 'serve' && !isPreview && mode !== 'test'
      ? createBackendProxy(process.env)
      : {},
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    exclude: ['**/node_modules/**', './e2e/**'],
  },
}))
