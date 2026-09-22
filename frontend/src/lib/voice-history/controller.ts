import { get, writable, type Readable } from 'svelte/store'

import { VoiceHistoryProjection } from '../../livekit/history-projection'
import type { TextSubmission } from '../../livekit/text-input'
import type { VoiceSessionContext } from '../../livekit/voice-session'
import type { SelectedConversationContext } from '../conversations/controller'
import type { ConversationTurn } from '../conversations/types'
import type { VoiceSessionEvent } from '../voice-session/generated'
import type {
  FailedVoiceTurnDisplay,
  SettledVoiceTurnDisplay,
  VoiceTurnDisplay,
} from '../voice-turn-display'

// 履歴の正本はBackend。ここで保持するのは確定イベントから作る画面表示用の一時状態であり、
// eventの所属contextを各turnが保持し、選択中contextへの投影はvisibleVoiceTurnsが行う。
export type LiveVoiceTurn = VoiceTurnDisplay & {
  context: SelectedConversationContext
  sourceUtteranceIds: string[]
  lastTextSequence: number
}

export type SettledVoiceTurn = LiveVoiceTurn & SettledVoiceTurnDisplay

export type FailedVoiceTurn = FailedVoiceTurnDisplay & {
  context: SelectedConversationContext
}

export type VoiceHistoryState = {
  live: LiveVoiceTurn | null
  settled: SettledVoiceTurn[]
  failed: FailedVoiceTurn[]
}

export type VoiceHistoryReceiveResult = {
  pendingInputAccepted: boolean
  pendingInputResolved: boolean
}

export type VoiceHistoryController = Readable<VoiceHistoryState> & {
  receive: (
    event: VoiceSessionEvent,
    voiceContext: VoiceSessionContext,
    submissions: readonly TextSubmission[],
  ) => VoiceHistoryReceiveResult
}

export type VoiceHistoryDependencies = {
  selectedContext: () => SelectedConversationContext | null
  refreshTurns: (context: SelectedConversationContext) => Promise<void>
  savedTurns: () => ConversationTurn[]
  refreshCharacter: (characterId: string) => Promise<void>
}

export const visibleVoiceTurns = (
  state: VoiceHistoryState,
  selected: { character: string; conversationId: string } | null,
): VoiceHistoryState => {
  if (selected === null) return { live: null, settled: [], failed: [] }
  const owned = (context: SelectedConversationContext) => (
    context.character === selected.character
    && context.conversationId === selected.conversationId
  )
  return {
    live: state.live !== null && owned(state.live.context) ? state.live : null,
    settled: state.settled.filter((turn) => owned(turn.context)),
    failed: state.failed.filter((turn) => owned(turn.context)),
  }
}

const publishState = (state: VoiceHistoryState): VoiceHistoryState => ({
  live: state.live === null ? null : {
    ...state.live,
    context: { ...state.live.context },
    sourceUtteranceIds: [...state.live.sourceUtteranceIds],
  },
  settled: state.settled.map((turn) => ({
    ...turn,
    context: { ...turn.context },
    sourceUtteranceIds: [...turn.sourceUtteranceIds],
  })),
  failed: state.failed.map((turn) => ({ ...turn, context: { ...turn.context } })),
})

export const createVoiceHistoryController = (
  dependencies: VoiceHistoryDependencies,
): VoiceHistoryController => {
  const projection = new VoiceHistoryProjection()
  const store = writable<VoiceHistoryState>({ live: null, settled: [], failed: [] })

  const eventContext = (voiceContext: VoiceSessionContext): SelectedConversationContext => {
    const selected = dependencies.selectedContext()
    if (selected?.character === voiceContext.characterId
      && selected.conversationId === voiceContext.conversationId) return selected
    return {
      character: voiceContext.characterId,
      conversationId: voiceContext.conversationId,
      version: -1,
    }
  }

  // 再取得・一覧更新の失敗では一時表示を消さない。失敗は各controller側のerrorで表現される。
  const retainOnRefreshFailure = (): void => {}

  const refreshSavedHistory = (context: SelectedConversationContext): void => {
    void dependencies.refreshTurns(context).catch(retainOnRefreshFailure)
    void dependencies.refreshCharacter(context.character).catch(retainOnRefreshFailure)
  }

  const confirmSettledTurns = (context: SelectedConversationContext): void => {
    void dependencies.refreshTurns(context).then(() => {
      const current = dependencies.selectedContext()
      if (current?.character !== context.character
        || current.conversationId !== context.conversationId
        || current.version !== context.version) return
      const savedIds = new Set(dependencies.savedTurns().map((turn) => turn.turn_id))
      store.update((state) => ({
        ...state,
        settled: state.settled.filter((turn) => !savedIds.has(turn.historyTurnId)),
      }))
    }).catch(retainOnRefreshFailure)
    void dependencies.refreshCharacter(context.character).catch(retainOnRefreshFailure)
  }

  const receive: VoiceHistoryController['receive'] = (event, voiceContext, submissions) => {
    const context = eventContext(voiceContext)
    const projected = projection.receive(event, voiceContext, submissions)

    if (event.type === 'utterance_finalized' && event.utterance_id !== undefined) {
      const utteranceId = event.utterance_id
      const transcript = event.transcript ?? ''
      if (event.should_response === false) {
        return { pendingInputAccepted: false, pendingInputResolved: false }
      }
      store.update((state) => {
        if (state.live === null) {
          return {
            ...state,
            live: {
              context,
              responseId: null,
              sourceUtteranceIds: [utteranceId],
              userContent: transcript,
              assistantContent: '',
              lastTextSequence: 0,
            },
          }
        }
        if (state.live.responseId === null) {
          return {
            ...state,
            live: {
              ...state.live,
              sourceUtteranceIds: [...state.live.sourceUtteranceIds, utteranceId],
              userContent: [state.live.userContent, transcript]
                .filter((text) => text !== '')
                .join('\n'),
            },
          }
        }
        return state
      })
      return { pendingInputAccepted: true, pendingInputResolved: false }
    }

    if (event.type === 'response_privacy_skipped' && event.response_id !== undefined) {
      const sourceIds = (event.source_inputs ?? [])
        .filter((source) => source.source === 'speech')
        .map((source) => source.input_id)
      let cleared = false
      store.update((state) => {
        const live = state.live
        if (live === null) return state
        const target = live.responseId === event.response_id
          || (live.responseId === null
            && live.sourceUtteranceIds.some((id) => sourceIds.includes(id)))
        if (!target) return state
        cleared = true
        return { ...state, live: null }
      })
      // 開始前に省略された入力にも保存済みのprivacy表示を反映する。
      refreshSavedHistory(context)
      return { pendingInputAccepted: false, pendingInputResolved: cleared }
    }

    if (event.type === 'response_started' && event.response_id !== undefined) {
      if (projected === null) return { pendingInputAccepted: false, pendingInputResolved: false }
      const responseId = event.response_id
      store.update((state) => ({
        ...state,
        live: {
          context,
          ...(event.history_turn_id === undefined
            ? {}
            : { historyTurnId: event.history_turn_id }),
          responseId,
          sourceUtteranceIds: event.source_utterance_ids ?? [],
          userContent: projected.userContent,
          assistantContent: '',
          lastTextSequence: 0,
        },
      }))
      return { pendingInputAccepted: false, pendingInputResolved: false }
    }

    if (event.type === 'response_delta' && event.response_id !== undefined) {
      if (projected === null) {
        return { pendingInputAccepted: false, pendingInputResolved: false }
      }
      const responseId = event.response_id
      const content = {
        assistantContent: projected.assistantContent,
        lastTextSequence: projected.lastTextSequence,
      }
      let applied = false
      store.update((state) => {
        const live = state.live
        if (live === null || live.responseId !== responseId) return state
        applied = true
        return { ...state, live: { ...live, ...content } }
      })
      return { pendingInputAccepted: false, pendingInputResolved: applied }
    }

    if (event.type === 'response_completed' || event.type === 'response_cancelled'
      || event.type === 'response_failed') {
      const responseId = event.response_id
      const live = get(store).live
      if (responseId === undefined || live === null || live.responseId !== responseId) {
        return { pendingInputAccepted: false, pendingInputResolved: false }
      }
      if (event.type === 'response_failed') {
        store.update((state) => ({
          ...state,
          live: null,
          failed: [...state.failed, {
            responseId,
            context: live.context,
            userContent: live.userContent,
            assistantContent: live.assistantContent,
          }],
        }))
        return { pendingInputAccepted: false, pendingInputResolved: true }
      }
      const historyTurnId = live.historyTurnId
      store.update((state) => ({
        ...state,
        live: null,
        settled: historyTurnId === undefined ? state.settled : [...state.settled, {
          ...live,
          historyTurnId,
          responseId,
          terminal: event.type === 'response_cancelled' ? 'cancelled' as const : 'completed' as const,
        }],
      }))
      confirmSettledTurns(live.context)
      return { pendingInputAccepted: false, pendingInputResolved: true }
    }

    if (event.type === 'utterance_discarded' && event.utterance_id !== undefined) {
      store.update((state) => (
        state.live?.responseId === null ? { ...state, live: null } : state
      ))
      return { pendingInputAccepted: false, pendingInputResolved: true }
    }

    return { pendingInputAccepted: false, pendingInputResolved: false }
  }

  return {
    subscribe: (run, invalidate) => store.subscribe(
      (state) => run(publishState(state)),
      invalidate,
    ),
    receive,
  }
}
