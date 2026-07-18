import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

import { describe, expect, test } from 'vitest'

describe('E2E support module public API', () => {
  test('keeps mock WebSocket frame details internal', async () => {
    const source = await readFile(join(process.cwd(), 'e2e/mock-web-socket.ts'), 'utf-8')

    expect(source).not.toContain('export type MockWebSocketFrame')
    expect(source).toContain('export const installMockWebSocketBackend')
  })
})
