import { get, writable, type Readable } from 'svelte/store'

import type { CharacterCatalogEntry } from '../characters/types'
import type { Conversation } from '../conversations/types'
import type { UiPreferencesPatch, UiSettings } from '../ui-settings/types'

export type ThreadMode = 'active' | 'archived'

export type CharacterThreadGroup = {
  character: CharacterCatalogEntry
  conversations: Conversation[]
  pinned: boolean
  pinOrder: number | null
  expanded: boolean
}

export type SidebarState = {
  catalog: CharacterCatalogEntry[]
  settings: UiSettings | null
  activeByCharacter: Record<string, Conversation[]>
  archivedByCharacter: Record<string, Conversation[]>
  archivedLoaded: string[]
  showingArchived: boolean
  expandedGroups: string[]
  pending: boolean
  initialized: boolean
  error: string | null
}

export type SidebarGateway = {
  listCatalog: () => Promise<CharacterCatalogEntry[]>
  rescanCatalog: () => Promise<CharacterCatalogEntry[]>
  getSettings: () => Promise<UiSettings>
  updatePreferences: (patch: UiPreferencesPatch) => Promise<UiSettings>
  setCharacterVisibility: (characterId: string, visible: boolean) => Promise<UiSettings>
  setCharacterPinned: (characterId: string, pinned: boolean) => Promise<UiSettings>
  setThreadPinned: (
    characterId: string,
    conversationId: string,
    pinned: boolean,
  ) => Promise<UiSettings>
  listActive: (characterId: string) => Promise<Conversation[]>
  listArchived: (characterId: string) => Promise<Conversation[]>
  create: (characterId: string) => Promise<Conversation>
  rename: (
    characterId: string,
    conversationId: string,
    title: string,
  ) => Promise<Conversation>
  archive: (characterId: string, conversationId: string) => Promise<Conversation>
  unarchive: (characterId: string, conversationId: string) => Promise<Conversation>
  hardDelete: (characterId: string, conversationId: string) => Promise<void>
}

export type SidebarController = Readable<SidebarState> & {
  initialize: () => Promise<void>
  rescan: () => Promise<void>
  updatePreferences: (patch: UiPreferencesPatch) => Promise<boolean>
  addCharacter: (characterId: string) => Promise<boolean>
  hideCharacter: (characterId: string) => Promise<void>
  setCharacterPinned: (characterId: string, pinned: boolean) => Promise<void>
  setThreadPinned: (
    characterId: string,
    conversationId: string,
    pinned: boolean,
  ) => Promise<void>
  showActive: () => void
  showArchived: () => Promise<void>
  toggleExpanded: (characterId: string) => void
  createConversation: (characterId: string) => Promise<Conversation | null>
  renameConversation: (
    characterId: string,
    conversationId: string,
    title: string,
  ) => Promise<boolean>
  archiveConversation: (
    characterId: string,
    conversationId: string,
  ) => Promise<boolean>
  unarchiveConversation: (characterId: string, conversationId: string) => Promise<void>
  hardDeleteConversation: (
    characterId: string,
    conversationId: string,
  ) => Promise<boolean>
  refreshCharacter: (characterId: string) => Promise<void>
}

const initialState = (): SidebarState => ({
  catalog: [],
  settings: null,
  activeByCharacter: {},
  archivedByCharacter: {},
  archivedLoaded: [],
  showingArchived: false,
  expandedGroups: [],
  pending: false,
  initialized: false,
  error: null,
})

const ordered = (items: Conversation[]): Conversation[] => [...items].sort(
  (left, right) => right.updated_at.localeCompare(left.updated_at)
    || right.conversation_id.localeCompare(left.conversation_id),
)

const visibleCharacterIds = (state: SidebarState): string[] => {
  if (state.settings === null) return []
  const catalogIds = new Set(state.catalog.map((item) => item.character_id))
  return state.settings.characters
    .filter((item) => item.visible && catalogIds.has(item.character_id))
    .map((item) => item.character_id)
}

const latestActivity = (items: Conversation[]): string => (
  ordered(items)[0]?.updated_at ?? ''
)

export const selectCharacterGroups = (state: SidebarState): CharacterThreadGroup[] => {
  if (state.settings === null) return []
  const mode: ThreadMode = state.showingArchived ? 'archived' : 'active'
  const catalog = new Map(state.catalog.map((item) => [item.character_id, item]))
  const source = state.showingArchived
    ? state.archivedByCharacter
    : state.activeByCharacter
  return state.settings.characters
    .filter((item) => item.visible && catalog.has(item.character_id))
    .map((item) => ({
      character: catalog.get(item.character_id) as CharacterCatalogEntry,
      conversations: orderThreadsForCharacter(
        source[item.character_id] ?? [],
        state.settings as UiSettings,
      ),
      pinned: item.pinned,
      pinOrder: item.pin_order,
      expanded: state.expandedGroups.includes(`${mode}:${item.character_id}`),
    }))
    .sort((left, right) => {
      if (left.pinned !== right.pinned) return left.pinned ? -1 : 1
      if (left.pinned && right.pinned) {
        return (left.pinOrder ?? 0) - (right.pinOrder ?? 0)
      }
      return latestActivity(right.conversations).localeCompare(
        latestActivity(left.conversations),
      ) || left.character.character_id.localeCompare(right.character.character_id)
    })
}

export const orderThreadsForCharacter = (
  conversations: Conversation[],
  settings: UiSettings,
): Conversation[] => {
  const pins = new Set(settings.thread_pins.map((item) => (
    `${item.character_id}/${item.conversation_id}`
  )))
  return [...conversations].sort((left, right) => {
    const leftPinned = pins.has(`${left.character_id}/${left.conversation_id}`)
    const rightPinned = pins.has(`${right.character_id}/${right.conversation_id}`)
    if (leftPinned !== rightPinned) return leftPinned ? -1 : 1
    return right.updated_at.localeCompare(left.updated_at)
      || right.conversation_id.localeCompare(left.conversation_id)
  })
}

export const collapsedThreads = (
  group: CharacterThreadGroup,
  settings: UiSettings,
): { visible: Conversation[], hiddenCount: number } => {
  if (group.expanded) return { visible: group.conversations, hiddenCount: 0 }
  const pins = new Set(settings.thread_pins.map((item) => (
    `${item.character_id}/${item.conversation_id}`
  )))
  const pinned = group.conversations.filter((item) => (
    pins.has(`${item.character_id}/${item.conversation_id}`)
  ))
  const unpinned = group.conversations.filter((item) => (
    !pins.has(`${item.character_id}/${item.conversation_id}`)
  ))
  const visible = [
    ...pinned,
    ...unpinned.slice(0, Math.max(0, 5 - pinned.length)),
  ]
  return { visible, hiddenCount: group.conversations.length - visible.length }
}

export const createSidebarController = (
  gateway: SidebarGateway,
  errorMessage: string,
): SidebarController => {
  const store = writable<SidebarState>(initialState())
  let operationVersion = 0
  let mutationVersion = 0
  const refreshVersions = new Map<string, number>()

  const fail = () => store.update((state) => ({
    ...state,
    pending: false,
    error: errorMessage,
  }))

  const loadLists = async (
    characterIds: string[],
    mode: ThreadMode,
  ): Promise<Record<string, Conversation[]>> => Object.fromEntries(
    await Promise.all(characterIds.map(async (characterId) => [
      characterId,
      ordered(await (mode === 'active'
        ? gateway.listActive(characterId)
        : gateway.listArchived(characterId))),
    ])),
  )

  const run = async <T>(operation: () => Promise<T>): Promise<T | null> => {
    if (get(store).pending) return null
    ++mutationVersion
    store.update((state) => ({ ...state, pending: true, error: null }))
    try {
      return await operation()
    } catch {
      fail()
      return null
    } finally {
      store.update((state) => ({ ...state, pending: false }))
    }
  }

  const controller: SidebarController = {
    subscribe: store.subscribe,
    initialize: async () => {
      const version = ++operationVersion
      ++mutationVersion
      store.set({ ...initialState(), pending: true })
      try {
        const [catalog, settings] = await Promise.all([
          gateway.listCatalog(),
          gateway.getSettings(),
        ])
        const partial = { ...initialState(), catalog, settings }
        const ids = visibleCharacterIds(partial)
        const activeByCharacter = await loadLists(ids, 'active')
        if (version !== operationVersion) return
        store.set({
          ...partial,
          activeByCharacter,
          pending: false,
          initialized: true,
        })
      } catch {
        if (version === operationVersion) fail()
      }
    },
    rescan: async () => {
      await run(async () => {
        const catalog = await gateway.rescanCatalog()
        const before = get(store)
        const candidate = { ...before, catalog }
        const missing = visibleCharacterIds(candidate).filter(
          (id) => !(id in before.activeByCharacter),
        )
        const added = await loadLists(missing, 'active')
        store.update((state) => ({
          ...state,
          catalog,
          activeByCharacter: { ...state.activeByCharacter, ...added },
        }))
      })
    },
    updatePreferences: async (patch) => {
      if (get(store).pending) return false
      const previous = get(store).settings
      if (previous === null) return false
      store.update((state) => ({
        ...state,
        settings: { ...previous, ...patch },
        pending: true,
        error: null,
      }))
      try {
        const settings = await gateway.updatePreferences(patch)
        store.update((state) => ({ ...state, settings }))
        return true
      } catch {
        store.update((state) => ({ ...state, settings: previous, error: errorMessage }))
        return false
      } finally {
        store.update((state) => ({ ...state, pending: false }))
      }
    },
    addCharacter: async (characterId) => (
      (await run(async () => {
        const settings = await gateway.setCharacterVisibility(characterId, true)
        const active = await gateway.listActive(characterId)
        const archived = get(store).showingArchived
          ? await gateway.listArchived(characterId)
          : null
        store.update((state) => ({
          ...state,
          settings,
          activeByCharacter: {
            ...state.activeByCharacter,
            [characterId]: ordered(active),
          },
          archivedByCharacter: archived === null
            ? state.archivedByCharacter
            : { ...state.archivedByCharacter, [characterId]: ordered(archived) },
          archivedLoaded: archived === null
            ? state.archivedLoaded
            : [...new Set([...state.archivedLoaded, characterId])],
        }))
        return true
      })) ?? false
    ),
    hideCharacter: async (characterId) => {
      await run(async () => {
        const settings = await gateway.setCharacterVisibility(characterId, false)
        store.update((state) => ({ ...state, settings }))
      })
    },
    setCharacterPinned: async (characterId, pinned) => {
      await run(async () => {
        const settings = await gateway.setCharacterPinned(characterId, pinned)
        store.update((state) => ({ ...state, settings }))
      })
    },
    setThreadPinned: async (characterId, conversationId, pinned) => {
      await run(async () => {
        const settings = await gateway.setThreadPinned(
          characterId,
          conversationId,
          pinned,
        )
        store.update((state) => ({ ...state, settings }))
      })
    },
    showActive: () => store.update((state) => ({
      ...state,
      showingArchived: false,
    })),
    showArchived: async () => {
      if (get(store).pending) return
      store.update((state) => ({ ...state, showingArchived: true }))
      await run(async () => {
        const state = get(store)
        const missing = visibleCharacterIds(state).filter(
          (id) => !state.archivedLoaded.includes(id),
        )
        const loaded = await loadLists(missing, 'archived')
        store.update((current) => ({
          ...current,
          archivedByCharacter: { ...current.archivedByCharacter, ...loaded },
          archivedLoaded: [...new Set([...current.archivedLoaded, ...missing])],
        }))
      })
    },
    toggleExpanded: (characterId) => store.update((state) => {
      const mode: ThreadMode = state.showingArchived ? 'archived' : 'active'
      const key = `${mode}:${characterId}`
      return {
        ...state,
        expandedGroups: state.expandedGroups.includes(key)
          ? state.expandedGroups.filter((item) => item !== key)
          : [...state.expandedGroups, key],
      }
    }),
    createConversation: async (characterId) => run(async () => {
      const created = await gateway.create(characterId)
      store.update((state) => ({
        ...state,
        activeByCharacter: {
          ...state.activeByCharacter,
          [characterId]: ordered([
            created,
            ...(state.activeByCharacter[characterId] ?? []),
          ]),
        },
      }))
      return created
    }),
    renameConversation: async (characterId, conversationId, title) => (
      (await run(async () => {
        const renamed = await gateway.rename(characterId, conversationId, title)
        store.update((state) => ({
          ...state,
          activeByCharacter: replaceConversation(
            state.activeByCharacter,
            renamed,
          ),
          archivedByCharacter: replaceConversation(
            state.archivedByCharacter,
            renamed,
          ),
        }))
        return true
      })) ?? false
    ),
    archiveConversation: async (characterId, conversationId) => (
      (await run(async () => {
        const archived = await gateway.archive(characterId, conversationId)
        store.update((state) => ({
          ...state,
          activeByCharacter: removeConversation(
            state.activeByCharacter,
            characterId,
            conversationId,
          ),
          archivedByCharacter: insertConversation(
            state.archivedByCharacter,
            archived,
          ),
        }))
        return true
      })) ?? false
    ),
    unarchiveConversation: async (characterId, conversationId) => {
      await run(async () => {
        const active = await gateway.unarchive(characterId, conversationId)
        store.update((state) => ({
          ...state,
          archivedByCharacter: removeConversation(
            state.archivedByCharacter,
            characterId,
            conversationId,
          ),
          activeByCharacter: insertConversation(state.activeByCharacter, active),
        }))
      })
    },
    hardDeleteConversation: async (characterId, conversationId) => (
      (await run(async () => {
        await gateway.hardDelete(characterId, conversationId)
        store.update((state) => ({
          ...state,
          settings: state.settings === null ? null : {
            ...state.settings,
            thread_pins: state.settings.thread_pins.filter((pin) => !(
              pin.character_id === characterId
              && pin.conversation_id === conversationId
            )),
          },
          activeByCharacter: removeConversation(
            state.activeByCharacter,
            characterId,
            conversationId,
          ),
          archivedByCharacter: removeConversation(
            state.archivedByCharacter,
            characterId,
            conversationId,
          ),
        }))
        return true
      })) ?? false
    ),
    refreshCharacter: async (characterId) => {
      if (get(store).pending) return
      const mutationAtStart = mutationVersion
      const refreshVersion = (refreshVersions.get(characterId) ?? 0) + 1
      refreshVersions.set(characterId, refreshVersion)
      try {
        const active = await gateway.listActive(characterId)
        const state = get(store)
        const archived = state.archivedLoaded.includes(characterId)
          ? await gateway.listArchived(characterId)
          : null
        if (
          mutationAtStart !== mutationVersion
          || refreshVersions.get(characterId) !== refreshVersion
        ) return
        store.update((current) => ({
          ...current,
          activeByCharacter: {
            ...current.activeByCharacter,
            [characterId]: ordered(active),
          },
          archivedByCharacter: archived === null
            ? current.archivedByCharacter
            : { ...current.archivedByCharacter, [characterId]: ordered(archived) },
        }))
      } catch {
        if (
          mutationAtStart === mutationVersion
          && refreshVersions.get(characterId) === refreshVersion
        ) store.update((state) => ({ ...state, error: errorMessage }))
      }
    },
  }
  return controller
}

const replaceConversation = (
  source: Record<string, Conversation[]>,
  conversation: Conversation,
): Record<string, Conversation[]> => {
  const current = source[conversation.character_id]
  if (current === undefined || !current.some(
    (item) => item.conversation_id === conversation.conversation_id,
  )) return source
  return {
    ...source,
    [conversation.character_id]: ordered(current.map((item) => (
      item.conversation_id === conversation.conversation_id ? conversation : item
    ))),
  }
}

const removeConversation = (
  source: Record<string, Conversation[]>,
  characterId: string,
  conversationId: string,
): Record<string, Conversation[]> => ({
  ...source,
  [characterId]: (source[characterId] ?? []).filter(
    (item) => item.conversation_id !== conversationId,
  ),
})

const insertConversation = (
  source: Record<string, Conversation[]>,
  conversation: Conversation,
): Record<string, Conversation[]> => ({
  ...source,
  [conversation.character_id]: ordered([
    conversation,
    ...(source[conversation.character_id] ?? []).filter(
      (item) => item.conversation_id !== conversation.conversation_id,
    ),
  ]),
})
