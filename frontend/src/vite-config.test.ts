// @vitest-environment node

import { describe, expect, test } from 'vitest'

import viteConfig from '../vite.config'


describe('vite config', () => {
  test('should keep preview available without a resolved profile report', async () => {
    if (typeof viteConfig !== 'function') {
      throw new TypeError('Vite config must be a function')
    }

    const config = await viteConfig({
      command: 'serve',
      mode: 'production',
      isPreview: true,
    })

    expect(config.server?.proxy).toEqual({})
  })
})
