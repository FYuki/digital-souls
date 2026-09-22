export const CHARACTERS_API_PREFIX = '/api/characters'

export const characterApiPath = (character: string): string => (
  `${CHARACTERS_API_PREFIX}/${encodeURIComponent(character)}`
)
