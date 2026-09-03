import type { Page } from '@playwright/test'

type MockCharacter = {
  character_id: string
  display_name: string
  standing_image?: {
    status: 'available' | 'missing'
    url: string | null
  }
}

type MockUiPreferences = {
  desktopPortraitLayout?: 'right' | 'background'
  desktopHistoryHeightPercent?: 50 | 75 | 100
  compactHistoryHeightPercent?: 50 | 75 | 100
}

export const installMockUiBootstrap = async (
  page: Page,
  characters: MockCharacter[] = [{ character_id: 'miori', display_name: '光織' }],
  preferences: MockUiPreferences = {},
): Promise<void> => {
  const visible = new Set(characters.map((character) => character.character_id))
  const pinnedCharacters: string[] = []
  const threadPins = new Set<string>()
  let desktopPortraitLayout: 'right' | 'background'
    = preferences.desktopPortraitLayout ?? 'right'
  let desktopHistoryHeightPercent: 50 | 75 | 100
    = preferences.desktopHistoryHeightPercent ?? 75
  let compactHistoryHeightPercent: 50 | 75 | 100
    = preferences.compactHistoryHeightPercent ?? 75

  const settings = () => ({
    user_id: 'local',
    desktop_portrait_layout: desktopPortraitLayout,
    desktop_history_height_percent: desktopHistoryHeightPercent,
    compact_history_height_percent: compactHistoryHeightPercent,
    characters: characters.map((character) => {
      const pinIndex = pinnedCharacters.indexOf(character.character_id)
      return {
        character_id: character.character_id,
        visible: visible.has(character.character_id),
        pinned: pinIndex >= 0,
        pin_order: pinIndex >= 0 ? pinIndex + 1 : null,
      }
    }),
    thread_pins: [...threadPins].map((key) => {
      const [character_id, conversation_id] = key.split('/')
      return { character_id, conversation_id }
    }),
  })

  await page.route('**/api/characters', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(characters.map((character) => ({
        ...character,
        standing_image: character.standing_image ?? { status: 'missing', url: null },
      }))),
    })
  })
  await page.route('**/api/ui-settings**', async (route) => {
    const request = route.request()
    const pathname = new URL(request.url()).pathname
    const segments = pathname.split('/')
    const characterId = segments[4]
    const conversationId = segments[6]
    if (pathname === '/api/ui-settings' && request.method() === 'PATCH') {
      const body = request.postDataJSON() as {
        desktop_portrait_layout?: 'right' | 'background'
        desktop_history_height_percent?: 50 | 75 | 100
        compact_history_height_percent?: 50 | 75 | 100
      }
      desktopPortraitLayout = body.desktop_portrait_layout ?? desktopPortraitLayout
      desktopHistoryHeightPercent = body.desktop_history_height_percent
        ?? desktopHistoryHeightPercent
      compactHistoryHeightPercent = body.compact_history_height_percent
        ?? compactHistoryHeightPercent
    } else if (characterId !== undefined && pathname.endsWith('/pin')) {
      if (conversationId !== undefined) {
        const key = `${characterId}/${conversationId}`
        if (request.method() === 'PUT') threadPins.add(key)
        else threadPins.delete(key)
      } else if (request.method() === 'PUT' && !pinnedCharacters.includes(characterId)) {
        pinnedCharacters.push(characterId)
      } else if (request.method() === 'DELETE') {
        const index = pinnedCharacters.indexOf(characterId)
        if (index >= 0) pinnedCharacters.splice(index, 1)
      }
    } else if (characterId !== undefined && request.method() === 'PUT') {
      const body = request.postDataJSON() as { visible?: boolean }
      if (body.visible === true) visible.add(characterId)
      else if (body.visible === false) visible.delete(characterId)
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(settings()),
    })
  })
}
