import type {
  CharacterUiState,
  HistoryHeightPercent,
  ThreadPin,
  UiPreferencesPatch,
  UiSettings,
} from './types'

const SETTINGS_PATH = '/api/ui-settings'
const CHARACTER_ID_PATTERN = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/
const UUID_V4_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
const HISTORY_HEIGHTS: ReadonlySet<number> = new Set([50, 75, 100])

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === 'object' && value !== null
)

const parseCharacter = (value: unknown): CharacterUiState => {
  if (
    !isRecord(value)
    || typeof value.character_id !== 'string'
    || !CHARACTER_ID_PATTERN.test(value.character_id)
    || typeof value.visible !== 'boolean'
    || typeof value.pinned !== 'boolean'
    || !(value.pin_order === null || (
      Number.isInteger(value.pin_order) && Number(value.pin_order) > 0
    ))
    || value.pinned !== (value.pin_order !== null)
  ) throw new Error('UI character settings response shape is invalid')
  return value as CharacterUiState
}

const parseThreadPin = (value: unknown): ThreadPin => {
  if (
    !isRecord(value)
    || typeof value.character_id !== 'string'
    || !CHARACTER_ID_PATTERN.test(value.character_id)
    || typeof value.conversation_id !== 'string'
    || !UUID_V4_PATTERN.test(value.conversation_id)
  ) throw new Error('UI thread pin response shape is invalid')
  return value as ThreadPin
}

const isHistoryHeight = (value: unknown): value is HistoryHeightPercent => (
  typeof value === 'number' && HISTORY_HEIGHTS.has(value)
)

const parseSettings = (value: unknown): UiSettings => {
  if (
    !isRecord(value)
    || typeof value.user_id !== 'string'
    || value.user_id.length === 0
    || !(value.desktop_portrait_layout === 'right'
      || value.desktop_portrait_layout === 'background')
    || !isHistoryHeight(value.desktop_history_height_percent)
    || !isHistoryHeight(value.compact_history_height_percent)
    || !Array.isArray(value.characters)
    || !Array.isArray(value.thread_pins)
  ) throw new Error('UI settings response shape is invalid')
  const characters = value.characters.map(parseCharacter)
  const threadPins = value.thread_pins.map(parseThreadPin)
  const characterIds = characters.map((item) => item.character_id)
  const pinOrders = characters.flatMap((item) => (
    item.pin_order === null ? [] : [item.pin_order]
  ))
  const threadPinKeys = threadPins.map((item) => (
    `${item.character_id}/${item.conversation_id}`
  ))
  if (
    new Set(characterIds).size !== characterIds.length
    || new Set(pinOrders).size !== pinOrders.length
    || new Set(threadPinKeys).size !== threadPinKeys.length
    || threadPins.some((pin) => !characterIds.includes(pin.character_id))
  ) throw new Error('UI settings response boundary is invalid')
  return { ...value, characters, thread_pins: threadPins } as UiSettings
}

const requestSettings = async (
  path: string,
  init?: RequestInit,
): Promise<UiSettings> => {
  const response = await fetch(path, init)
  if (!response.ok) {
    throw new Error(`UI settings request failed with status ${response.status}`)
  }
  return parseSettings(await response.json())
}

export const getUiSettings = async (): Promise<UiSettings> => (
  requestSettings(SETTINGS_PATH)
)

export const updateUiPreferences = async (
  patch: UiPreferencesPatch,
): Promise<UiSettings> => requestSettings(SETTINGS_PATH, {
  method: 'PATCH',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(patch),
})

const characterPath = (characterId: string): string => (
  `${SETTINGS_PATH}/characters/${encodeURIComponent(characterId)}`
)

export const setCharacterVisibility = async (
  characterId: string,
  visible: boolean,
): Promise<UiSettings> => requestSettings(characterPath(characterId), {
  method: 'PUT',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ visible }),
})

export const setCharacterPinned = async (
  characterId: string,
  pinned: boolean,
): Promise<UiSettings> => requestSettings(
  `${characterPath(characterId)}/pin`,
  { method: pinned ? 'PUT' : 'DELETE' },
)

export const setThreadPinned = async (
  characterId: string,
  conversationId: string,
  pinned: boolean,
): Promise<UiSettings> => requestSettings(
  `${characterPath(characterId)}/conversations/${encodeURIComponent(conversationId)}/pin`,
  { method: pinned ? 'PUT' : 'DELETE' },
)
