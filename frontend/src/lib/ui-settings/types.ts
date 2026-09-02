export type PortraitLayout = 'right' | 'background'
export type HistoryHeightPercent = 50 | 75 | 100

export type CharacterUiState = {
  character_id: string
  visible: boolean
  pinned: boolean
  pin_order: number | null
}

export type ThreadPin = {
  character_id: string
  conversation_id: string
}

export type UiSettings = {
  user_id: string
  desktop_portrait_layout: PortraitLayout
  desktop_history_height_percent: HistoryHeightPercent
  compact_history_height_percent: HistoryHeightPercent
  characters: CharacterUiState[]
  thread_pins: ThreadPin[]
}

export type UiPreferencesPatch = Partial<Pick<
  UiSettings,
  | 'desktop_portrait_layout'
  | 'desktop_history_height_percent'
  | 'compact_history_height_percent'
>>
