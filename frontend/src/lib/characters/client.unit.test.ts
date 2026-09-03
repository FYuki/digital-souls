import { afterEach, describe, expect, test, vi } from 'vitest'

import { listCharacters, rescanCharacters } from './client'

const available = {
  character_id: 'miori',
  display_name: '光織',
  standing_image: {
    status: 'available',
    url: '/api/characters/miori/assets/standing/default.png',
  },
}

const respondWith = (body: unknown, status = 200): void => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(
    JSON.stringify(body),
    { status },
  )))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('character catalog client', () => {
  test('loads a freshly scanned catalog on browser initialization', async () => {
    respondWith([available])

    await expect(listCharacters()).resolves.toEqual([available])
    expect(fetch).toHaveBeenCalledWith('/api/characters', undefined)
  })

  test('requests an explicit rescan', async () => {
    respondWith([available])

    await expect(rescanCharacters()).resolves.toEqual([available])
    expect(fetch).toHaveBeenCalledWith('/api/characters/rescan', { method: 'POST' })
  })

  test('accepts an explicitly missing standing image', async () => {
    const missing = {
      ...available,
      standing_image: { status: 'missing', url: null },
    }
    respondWith([missing])

    await expect(listCharacters()).resolves.toEqual([missing])
  })

  test('rejects an unsafe standing image URL', async () => {
    respondWith([{
      ...available,
      standing_image: {
        status: 'available',
        url: '/api/characters/other/assets/standing/default.png',
      },
    }])

    await expect(listCharacters()).rejects.toThrow('standing image')
  })

  test('rejects invalid and duplicate character ids', async () => {
    respondWith([available, available])
    await expect(listCharacters()).rejects.toThrow('duplicate')

    respondWith([{ ...available, character_id: '../miori' }])
    await expect(listCharacters()).rejects.toThrow('shape')
  })

  test('rejects non-success responses', async () => {
    respondWith({ detail: 'unavailable' }, 503)

    await expect(listCharacters()).rejects.toThrow('status 503')
  })
})
