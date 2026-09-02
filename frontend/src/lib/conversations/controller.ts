import { get, writable, type Readable } from 'svelte/store'

import type { ConversationSessionManager } from '../conversation-session'
import type { Conversation, ConversationTurn } from './types'

export type ConversationGateway = {
  listActive: (character: string) => Promise<Conversation[]>
  listArchived: (character: string) => Promise<Conversation[]>
  listTurns: (character: string, conversationId: string) => Promise<ConversationTurn[]>
  create: (character: string) => Promise<Conversation>
  archive: (character: string, conversationId: string) => Promise<Conversation>
  unarchive: (character: string, conversationId: string) => Promise<Conversation>
  hardDelete: (character: string, conversationId: string) => Promise<void>
}

export type SelectedConversationContext = {
  character: string
  conversationId: string
  version: number
}

type CharacterContext = {
  character: string
  version: number
}

export type ConversationControllerState = {
  character: string
  active: Conversation[]
  archived: Conversation[]
  selectedConversationId: string | null
  turns: ConversationTurn[]
  showingArchived: boolean
  deleteCandidate: string | null
  pending: boolean
  error: string | null
}

export type ConversationController = Readable<ConversationControllerState> & {
  loadCharacter: (character: string) => Promise<void>
  selectConversation: (conversationId: string) => Promise<void>
  createConversation: () => Promise<void>
  archiveConversation: (conversationId: string) => Promise<void>
  unarchiveConversation: (conversationId: string) => Promise<void>
  showActive: () => void
  showArchived: () => Promise<void>
  requestHardDelete: (conversationId: string) => void
  cancelHardDelete: () => void
  confirmHardDelete: () => Promise<void>
  clearSelection: (character: string, conversationId: string) => void
  selectedContext: () => SelectedConversationContext | null
  appendTurn: (context: SelectedConversationContext, turn: ConversationTurn) => void
  refreshTurns: (context: SelectedConversationContext) => Promise<void>
  reportConversationError: (context: SelectedConversationContext) => void
}

const orderConversations = (conversations: Conversation[]): Conversation[] => (
  [...conversations].sort((left, right) => (
    right.updated_at.localeCompare(left.updated_at)
    || right.conversation_id.localeCompare(left.conversation_id)
  ))
)

const mergeLoadedHistory = (
  history: ConversationTurn[],
  receivedTurns: ConversationTurn[],
): ConversationTurn[] => {
  const historyTurnIds = new Set(history.map((turn) => turn.turn_id))
  return [...history, ...receivedTurns.filter((turn) => !historyTurnIds.has(turn.turn_id))]
}

const copyPublishedState = (state: ConversationControllerState): ConversationControllerState => ({
  ...state,
  active: state.active.map((conversation) => ({ ...conversation })),
  archived: state.archived.map((conversation) => ({ ...conversation })),
  turns: state.turns.map((turn) => ({ ...turn })),
})

export const createConversationController = (
  initialCharacter: string,
  errorMessage: string,
  gateway: ConversationGateway,
  sessions: ConversationSessionManager,
): ConversationController => {
  const store = writable<ConversationControllerState>({
    character: initialCharacter,
    active: [],
    archived: [],
    selectedConversationId: null,
    turns: [],
    showingArchived: false,
    deleteCandidate: null,
    pending: false,
    error: null,
  })
  let contextVersion = 0
  let listVersion = 0

  const updateIfCurrent = (version: number, update: (state: ConversationControllerState) => ConversationControllerState) => {
    if (version === contextVersion) store.update(update)
  }

  const updateIfListCurrent = (
    version: number,
    expectedListVersion: number,
    update: (state: ConversationControllerState) => ConversationControllerState,
  ) => {
    if (version === contextVersion && expectedListVersion === listVersion) store.update(update)
  }

  const failCurrent = (version: number) => {
    updateIfCurrent(version, (state) => ({ ...state, error: errorMessage }))
  }

  const isCharacterContextCurrent = (context: CharacterContext): boolean => (
    context.version === contextVersion && context.character === get(store).character
  )

  const selectedContext = (): SelectedConversationContext | null => {
    const state = get(store)
    if (state.selectedConversationId === null) return null
    return {
      character: state.character,
      conversationId: state.selectedConversationId,
      version: contextVersion,
    }
  }

  const isSelectedContextCurrent = (context: SelectedConversationContext): boolean => {
    const state = get(store)
    return context.version === contextVersion
      && context.character === state.character
      && context.conversationId === state.selectedConversationId
  }

  const clearSelectedState = (conversationId: string) => {
    store.update((state) => state.selectedConversationId === conversationId
      ? { ...state, selectedConversationId: null, turns: [], error: null }
      : state)
  }

  const clearSelection = (character: string, conversationId: string) => {
    sessions.clearConversation(character, conversationId)
    clearSelectedState(conversationId)
  }

  const selectConversationForContext = async (
    context: CharacterContext,
    conversationId: string,
  ) => {
    if (!isCharacterContextCurrent(context)) return
    sessions.selectConversation(context.character, conversationId)
    store.update((state) => ({ ...state, selectedConversationId: conversationId, turns: [], error: null }))
    try {
      const history = await gateway.listTurns(context.character, conversationId)
      updateIfCurrent(context.version, (state) => state.selectedConversationId === conversationId
        ? { ...state, turns: mergeLoadedHistory(history, state.turns) }
        : state)
    } catch {
      if (isSelectedContextCurrent({ ...context, conversationId })) failCurrent(context.version)
    }
  }

  const selectConversation = async (conversationId: string) => {
    const state = get(store)
    await selectConversationForContext(
      { character: state.character, version: contextVersion },
      conversationId,
    )
  }

  const loadCharacter = async (character: string) => {
    const version = ++contextVersion
    const expectedListVersion = ++listVersion
    store.set({
      character,
      active: [],
      archived: [],
      selectedConversationId: null,
      turns: [],
      showingArchived: false,
      deleteCandidate: null,
      pending: false,
      error: null,
    })
    try {
      const conversations = await gateway.listActive(character)
      updateIfListCurrent(version, expectedListVersion, (state) => ({ ...state, active: conversations }))
      if (version !== contextVersion || expectedListVersion !== listVersion) return
      const restoredId = sessions.getSelectedConversationId(character)
      if (restoredId !== null && conversations.some((item) => item.conversation_id === restoredId)) {
        await selectConversationForContext({ character, version }, restoredId)
      }
    } catch {
      updateIfListCurrent(version, expectedListVersion, (state) => ({ ...state, error: errorMessage }))
    }
  }

  const runLifecycle = async (operation: (context: CharacterContext) => Promise<void>) => {
    const state = get(store)
    if (state.pending || state.deleteCandidate !== null) return
    const context = { character: state.character, version: contextVersion }
    store.update((current) => ({ ...current, pending: true, error: null }))
    try {
      await operation(context)
    } catch {
      failCurrent(context.version)
    } finally {
      updateIfCurrent(context.version, (current) => ({ ...current, pending: false }))
    }
  }

  const controller: ConversationController = {
    subscribe: (run, invalidate) => store.subscribe(
      (state) => run(copyPublishedState(state)),
      invalidate,
    ),
    loadCharacter,
    selectConversation,
    createConversation: () => runLifecycle(async (context) => {
      const created = await gateway.create(context.character)
      if (!isCharacterContextCurrent(context)) return
      listVersion += 1
      store.update((state) => ({ ...state, active: orderConversations([created, ...state.active]) }))
      await selectConversationForContext(context, created.conversation_id)
    }),
    archiveConversation: (conversationId) => runLifecycle(async (context) => {
      const archived = await gateway.archive(context.character, conversationId)
      if (!isCharacterContextCurrent(context)) return
      listVersion += 1
      store.update((state) => ({
        ...state,
        active: state.active.filter((item) => item.conversation_id !== conversationId),
        archived: orderConversations([
          archived,
          ...state.archived.filter((item) => item.conversation_id !== conversationId),
        ]),
      }))
      clearSelection(context.character, conversationId)
    }),
    unarchiveConversation: (conversationId) => runLifecycle(async (context) => {
      const active = await gateway.unarchive(context.character, conversationId)
      if (!isCharacterContextCurrent(context)) return
      listVersion += 1
      store.update((state) => ({
        ...state,
        archived: state.archived.filter((item) => item.conversation_id !== conversationId),
        active: orderConversations([
          active,
          ...state.active.filter((item) => item.conversation_id !== conversationId),
        ]),
      }))
    }),
    showActive: () => store.update((state) => ({ ...state, showingArchived: false })),
    showArchived: async () => {
      const state = get(store)
      if (state.pending || state.deleteCandidate !== null) return
      const version = contextVersion
      const expectedListVersion = listVersion
      store.update((current) => ({ ...current, showingArchived: true }))
      try {
        const archived = await gateway.listArchived(state.character)
        updateIfListCurrent(version, expectedListVersion, (current) => ({ ...current, archived }))
      } catch {
        updateIfListCurrent(version, expectedListVersion, (current) => ({ ...current, error: errorMessage }))
      }
    },
    requestHardDelete: (conversationId) => {
      const state = get(store)
      if (state.pending || state.deleteCandidate !== null) return
      store.update((current) => ({ ...current, deleteCandidate: conversationId }))
    },
    cancelHardDelete: () => store.update((state) => state.pending
      ? state
      : { ...state, deleteCandidate: null }),
    confirmHardDelete: async () => {
      const state = get(store)
      if (state.deleteCandidate === null || state.pending) return
      const conversationId = state.deleteCandidate
      const context = { character: state.character, version: contextVersion }
      store.update((current) => ({ ...current, pending: true, error: null }))
      try {
        await gateway.hardDelete(context.character, conversationId)
        sessions.clearConversation(context.character, conversationId)
        if (!isCharacterContextCurrent(context)) return
        listVersion += 1
        clearSelectedState(conversationId)
        updateIfCurrent(context.version, (current) => ({
          ...current,
          active: current.active.filter((item) => item.conversation_id !== conversationId),
          archived: current.archived.filter((item) => item.conversation_id !== conversationId),
          deleteCandidate: null,
        }))
      } catch {
        failCurrent(context.version)
      } finally {
        updateIfCurrent(context.version, (current) => ({ ...current, pending: false }))
      }
    },
    clearSelection: (character, conversationId) => {
      const state = get(store)
      if (
        state.character !== character
        || state.selectedConversationId !== conversationId
      ) {
        sessions.clearConversation(character, conversationId)
        return
      }
      contextVersion += 1
      listVersion += 1
      clearSelection(character, conversationId)
    },
    selectedContext,
    appendTurn: (context, turn) => {
      if (!isSelectedContextCurrent(context)) return
      store.update((state) => ({ ...state, turns: [...state.turns, turn] }))
    },
    refreshTurns: async (context) => {
      if (!isSelectedContextCurrent(context)) return
      try {
        const history = await gateway.listTurns(
          context.character,
          context.conversationId,
        )
        if (!isSelectedContextCurrent(context)) return
        store.update((state) => ({
          ...state,
          turns: mergeLoadedHistory(history, state.turns),
        }))
      } catch {
        if (isSelectedContextCurrent(context)) failCurrent(context.version)
      }
    },
    reportConversationError: (context) => {
      if (isSelectedContextCurrent(context)) failCurrent(context.version)
    },
  }
  return controller
}
