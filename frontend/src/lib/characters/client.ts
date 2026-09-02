import type { CharacterCatalogEntry } from './types'

const CATALOG_PATH = '/api/characters'
const CHARACTER_ID_PATTERN = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === 'object' && value !== null
)

const parseEntry = (value: unknown): CharacterCatalogEntry => {
  if (
    !isRecord(value)
    || typeof value.character_id !== 'string'
    || !CHARACTER_ID_PATTERN.test(value.character_id)
    || typeof value.display_name !== 'string'
    || value.display_name.length === 0
    || !isRecord(value.standing_image)
  ) throw new Error('Character catalog response shape is invalid')

  const status = value.standing_image.status
  const url = value.standing_image.url
  const expectedUrl = `${CATALOG_PATH}/${value.character_id}/assets/standing/default.png`
  if (
    !(status === 'available' || status === 'missing')
    || (status === 'available' && url !== expectedUrl)
    || (status === 'missing' && url !== null)
  ) throw new Error('Character standing image response boundary is invalid')

  return value as CharacterCatalogEntry
}

const requestCatalog = async (init?: RequestInit): Promise<CharacterCatalogEntry[]> => {
  const response = await fetch(
    init === undefined ? CATALOG_PATH : `${CATALOG_PATH}/rescan`,
    init,
  )
  if (!response.ok) {
    throw new Error(`Character catalog request failed with status ${response.status}`)
  }
  const value: unknown = await response.json()
  if (!Array.isArray(value)) {
    throw new Error('Character catalog response shape is invalid')
  }
  const entries = value.map(parseEntry)
  if (new Set(entries.map((entry) => entry.character_id)).size !== entries.length) {
    throw new Error('Character catalog contains duplicate ids')
  }
  return entries
}

// 初回表示時にもGET側でcharactersディレクトリを再走査する。
export const listCharacters = async (): Promise<CharacterCatalogEntry[]> => (
  requestCatalog()
)

export const rescanCharacters = async (): Promise<CharacterCatalogEntry[]> => (
  requestCatalog({ method: 'POST' })
)
