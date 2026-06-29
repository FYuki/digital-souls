import { svelte, vitePreprocess } from '@sveltejs/vite-plugin-svelte'
import { defineConfig } from 'vite'

import { API_PROXY_PREFIX, apiProxyConfig } from './vite.proxy'

export default defineConfig({
  plugins: [svelte({ preprocess: vitePreprocess() })],
  server: {
    proxy: {
      [API_PROXY_PREFIX]: apiProxyConfig,
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
    exclude: ['**/node_modules/**', './e2e/**'],
  },
})
