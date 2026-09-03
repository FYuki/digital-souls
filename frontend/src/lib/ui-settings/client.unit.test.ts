import { afterEach, describe, expect, test, vi } from 'vitest'

import {
  getUiSettings,
  setCharacterPinned,
  setCharacterVisibility,
  setThreadPinned,
  updateUiPreferences,
} from './client'

const CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const settings = {
  user_id: 'local',
  desktop_portrait_layout: 'right',
  desktop_history_height_percent: 75,
  compact_history_height_percent: 75,
  characters: [{
    character_id: 'miori',
    visible: true,
    pinned: false,
    pin_order: null,
  }],
  thread_pins: [],
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

describe('UI settings client', () => {
  test('loads persisted settings', async () => {
    respondWith(settings)

    await expect(getUiSettings()).resolves.toEqual(settings)
    expect(fetch).toHaveBeenCalledWith('/api/ui-settings', undefined)
  })

  test('updates preferences through a partial patch', async () => {
    const updated = {
      ...settings,
      desktop_portrait_layout: 'background',
      desktop_history_height_percent: 50,
    }
    respondWith(updated)

    await expect(updateUiPreferences({
      desktop_portrait_layout: 'background',
      desktop_history_height_percent: 50,
    })).resolves.toEqual(updated)
    expect(fetch).toHaveBeenCalledWith('/api/ui-settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        desktop_portrait_layout: 'background',
        desktop_history_height_percent: 50,
      }),
    })
  })

  test('updates character visibility', async () => {
    respondWith(settings)

    await setCharacterVisibility('miori', false)

    expect(fetch).toHaveBeenCalledWith('/api/ui-settings/characters/miori', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ visible: false }),
    })
  })

  test('uses idempotent pin endpoints for characters and threads', async () => {
    respondWith(settings)

    await setCharacterPinned('miori', true)
    await setCharacterPinned('miori', false)
    await setThreadPinned('miori', CONVERSATION_ID, true)

    expect(fetch).toHaveBeenNthCalledWith(
      1,
      '/api/ui-settings/characters/miori/pin',
      { method: 'PUT' },
    )
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      '/api/ui-settings/characters/miori/pin',
      { method: 'DELETE' },
    )
    expect(fetch).toHaveBeenNthCalledWith(
      3,
      `/api/ui-settings/characters/miori/conversations/${CONVERSATION_ID}/pin`,
      { method: 'PUT' },
    )
  })

  test('rejects inconsistent pin metadata', async () => {
    respondWith({
      ...settings,
      characters: [{
        ...settings.characters[0],
        pinned: true,
        pin_order: null,
      }],
    })

    await expect(getUiSettings()).rejects.toThrow('character settings')
  })

  test('rejects a thread pin outside the configured character boundary', async () => {
    respondWith({
      ...settings,
      thread_pins: [{
        character_id: 'akira',
        conversation_id: CONVERSATION_ID,
      }],
    })

    await expect(getUiSettings()).rejects.toThrow('boundary')
  })

  test('rejects unsupported percentages and failed writes', async () => {
    respondWith({ ...settings, compact_history_height_percent: 60 })
    await expect(getUiSettings()).rejects.toThrow('shape')

    respondWith({ detail: 'failed' }, 500)
    await expect(setCharacterPinned('miori', true)).rejects.toThrow('status 500')
  })
})
