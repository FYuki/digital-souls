import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte'
import { defineConfig } from 'vite'

import { createVadAssetPlugin } from './vite.vad-assets'
import {
  API_PROXY_PREFIX,
  WS_PROXY_PREFIX,
  apiProxyConfig,
  wsProxyConfig,
} from './vite.proxy'

export default defineConfig({
  plugins: [svelte({ preprocess: vitePreprocess() }), createVadAssetPlugin()],
  resolve: {
    conditions: ['browser'],
  },
  server: {
    proxy: {
      [API_PROXY_PREFIX]: apiProxyConfig,
      [WS_PROXY_PREFIX]: wsProxyConfig,
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    exclude: ['**/node_modules/**', './e2e/**'],
  },
})
