export type StandingImageMetadata = {
  status: 'available' | 'missing'
  url: string | null
}

export type CharacterCatalogEntry = {
  character_id: string
  display_name: string
  standing_image: StandingImageMetadata
}
