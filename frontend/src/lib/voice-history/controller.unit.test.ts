import { get } from 'svelte/store'
import { describe, expect, test, vi } from 'vitest'

import type { TextSubmission } from '../../livekit/text-input'
import type { VoiceSessionContext } from '../../livekit/voice-session'
import type { SelectedConversationContext } from '../conversations/controller'
import type { ConversationTurn } from '../conversations/types'
import type { VoiceSessionEvent } from '../voice-session/generated'
import { parseVoiceSessionEvent } from '../voice-session/validation'
import {
  createVoiceHistoryController,
  visibleVoiceTurns,
  type VoiceHistoryController,
} from './controller'

// 音声履歴表示controllerの単体試験。Appをmountせず、実際のevent契約に沿った入力から
// 表示状態（進行中・確定待ち・失敗turn）と履歴再取得の調停を直接検証する。

const SESSION_ID = '20000000-0000-4000-8000-000000000001'
const PARTICIPANT_ID = '40000000-0000-4000-8000-000000000001'
const UTTERANCE_ID = '30000000-0000-4000-8000-000000000001'
const SECOND_UTTERANCE_ID = '30000000-0000-4000-8000-000000000002'
const TEXT_INPUT_ID = '60000000-0000-4000-8000-000000000001'
const RESPONSE_ID = '50000000-0000-4000-8000-000000000001'
const SECOND_RESPONSE_ID = '50000000-0000-4000-8000-000000000002'
const UNRELATED_RESPONSE_ID = '50000000-0000-4000-8000-000000000099'
const HISTORY_TURN_ID = '9e70795d-e5d5-431d-baa2-67f884403010'
const SECOND_HISTORY_TURN_ID = '9e70795d-e5d5-431d-baa2-67f884403020'

const contextA: VoiceSessionContext = { characterId: 'miori', conversationId: 'conversation-a' }
const contextAkari: VoiceSessionContext = { characterId: 'akari', conversationId: 'conversation-c' }

const viewA = { character: 'miori', conversationId: 'conversation-a' }
const viewB = { character: 'miori', conversationId: 'conversation-b' }
const viewAkari = { character: 'akari', conversationId: 'conversation-c' }

const selectionA = (version = 1): SelectedConversationContext => ({
  character: 'miori',
  conversationId: 'conversation-a',
  version,
})
const selectionB = (version = 2): SelectedConversationContext => ({
  character: 'miori',
  conversationId: 'conversation-b',
  version,
})

let eventSequence = 0
const voiceEvent = (fields: Record<string, unknown>): VoiceSessionEvent => parseVoiceSessionEvent({
  protocol_version: '2.0',
  event_id: `10000000-0000-4000-8000-${String(++eventSequence).padStart(12, '0')}`,
  session_id: SESSION_ID,
  monotonic_timestamp_ms: 1_000,
  ...fields,
})

const utteranceFinalized = (
  utteranceId: string,
  transcript: string,
  shouldResponse = true,
): VoiceSessionEvent => voiceEvent({
  type: 'utterance_finalized',
  utterance_id: utteranceId,
  transcript,
  should_response: shouldResponse,
  speaker: { participant_id: PARTICIPANT_ID, role: 'user' },
})

const responseStarted = (
  responseId: string,
  options: {
    historyTurnId?: string
    speechSources?: string[]
    sourceInputs?: { input_id: string; source: 'speech' | 'text' }[]
  } = {},
): VoiceSessionEvent => voiceEvent({
  type: 'response_started',
  response_id: responseId,
  speaker: { participant_id: PARTICIPANT_ID, role: 'character', character_id: 'miori' },
  source_utterance_ids: options.speechSources ?? [UTTERANCE_ID],
  ...(options.historyTurnId === undefined ? {} : { history_turn_id: options.historyTurnId }),
  ...(options.sourceInputs === undefined ? {} : { source_inputs: options.sourceInputs }),
})

const responseDelta = (
  responseId: string,
  sequence: number,
  text: string,
): VoiceSessionEvent => voiceEvent({
  type: 'response_delta',
  response_id: responseId,
  text_sequence: sequence,
  text,
  text_range: { start: 0, end: text.length },
})

const responseTerminal = (
  type: 'response_completed' | 'response_cancelled' | 'response_failed',
  responseId: string,
): VoiceSessionEvent => {
  if (type === 'response_completed') {
    return voiceEvent({ type, response_id: responseId, last_text_sequence: 1, last_audio_sequence: 0 })
  }
  if (type === 'response_cancelled') {
    return voiceEvent({ type, response_id: responseId, reason: 'barge_in' })
  }
  return voiceEvent({
    type, response_id: responseId, error_code: 'streaming_pipeline_failed', recoverable: false,
  })
}

const privacySkipped = (
  responseId: string,
  sourceInputs: { input_id: string; source: 'speech' | 'text' }[],
): VoiceSessionEvent => voiceEvent({
  type: 'response_privacy_skipped',
  response_id: responseId,
  source_inputs: sourceInputs,
})

const utteranceDiscarded = (utteranceId: string): VoiceSessionEvent => voiceEvent({
  type: 'utterance_discarded',
  utterance_id: utteranceId,
  reason: 'privacy',
})

const deferred = <T,>() => {
  let resolve: (value: T | PromiseLike<T>) => void = () => {
    throw new Error('resolver is not initialized')
  }
  const promise = new Promise<T>((settle) => {
    resolve = settle
  })
  return { promise, resolve }
}

const flush = () => new Promise<void>((resolve) => {
  setTimeout(resolve, 0)
})

const savedTurn = (turnId: string): ConversationTurn => ({
  kind: 'content',
  turn_id: turnId,
  user_content: '保存済みの質問',
  assistant_content: '保存済みの回答',
})

const createHarness = () => {
  let selection: SelectedConversationContext | null = null
  let saved: ConversationTurn[] = []
  const refreshTurns = vi.fn(async (_context: SelectedConversationContext) => {})
  const refreshCharacter = vi.fn(async (_characterId: string) => {})
  const controller = createVoiceHistoryController({
    selectedContext: () => selection,
    refreshTurns,
    savedTurns: () => saved,
    refreshCharacter,
  })
  return {
    controller,
    refreshTurns,
    refreshCharacter,
    select: (next: SelectedConversationContext | null) => {
      selection = next
    },
    saveTurns: (turns: ConversationTurn[]) => {
      saved = turns
    },
  }
}

const view = (
  controller: VoiceHistoryController,
  selected: { character: string; conversationId: string } | null,
) => visibleVoiceTurns(get(controller), selected)

describe('VoiceHistoryController', () => {
  test('accepts a response-bound utterance as a pending turn and merges a follow-up utterance', () => {
    const { controller } = createHarness()

    const accepted = controller.receive(
      utteranceFinalized(UTTERANCE_ID, '最初の質問'), contextA, [],
    )

    expect(accepted).toEqual({ pendingInputAccepted: true, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({
      responseId: null,
      userContent: '最初の質問',
      assistantContent: '',
    })

    controller.receive(utteranceFinalized(SECOND_UTTERANCE_ID, '続きの質問'), contextA, [])

    expect(get(controller).live).toMatchObject({
      responseId: null,
      userContent: '最初の質問\n続きの質問',
    })
  })

  test('keeps a queued utterance out of the live turn while a response is generating', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '最初の質問'), contextA, [])
    controller.receive(responseStarted(RESPONSE_ID), contextA, [])

    const queued = controller.receive(
      utteranceFinalized(SECOND_UTTERANCE_ID, '応答中の発話'), contextA, [],
    )

    expect(queued).toEqual({ pendingInputAccepted: true, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({
      responseId: RESPONSE_ID,
      userContent: '最初の質問',
    })

    // 待ちに入った発話は破棄されず、次のresponseの入力源として本文へ現れる。
    controller.receive(responseTerminal('response_completed', RESPONSE_ID), contextA, [])
    controller.receive(
      responseStarted(SECOND_RESPONSE_ID, { speechSources: [SECOND_UTTERANCE_ID] }),
      contextA,
      [],
    )
    expect(get(controller).live).toMatchObject({
      responseId: SECOND_RESPONSE_ID,
      userContent: '応答中の発話',
    })
  })

  test('feeds a non-response utterance to the projection without showing it', () => {
    const { controller } = createHarness()

    const ignored = controller.receive(
      utteranceFinalized(UTTERANCE_ID, '補足の発話', false), contextA, [],
    )

    expect(ignored).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toBeNull()

    controller.receive(
      responseStarted(RESPONSE_ID, { speechSources: [UTTERANCE_ID] }), contextA, [],
    )

    expect(get(controller).live).toMatchObject({
      responseId: RESPONSE_ID,
      userContent: '補足の発話',
    })
  })

  test('composes speech and text sources into the live turn content', () => {
    const { controller } = createHarness()
    const submission: TextSubmission = {
      inputId: TEXT_INPUT_ID,
      sessionId: SESSION_ID,
      context: contextA,
      text: 'テキストの質問',
      status: 'accepted',
      responseId: null,
      errorCode: null,
    }
    controller.receive(utteranceFinalized(UTTERANCE_ID, '音声の補足'), contextA, [submission])

    controller.receive(responseStarted(RESPONSE_ID, {
      speechSources: [UTTERANCE_ID],
      sourceInputs: [
        { input_id: UTTERANCE_ID, source: 'speech' },
        { input_id: TEXT_INPUT_ID, source: 'text' },
      ],
    }), contextA, [submission])

    expect(get(controller).live).toMatchObject({
      responseId: RESPONSE_ID,
      userContent: '音声の補足\nテキストの質問',
    })
  })

  test('replaces the pending display with the projected turn when the response starts', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '一つ目の質問'), contextA, [])
    controller.receive(utteranceFinalized(SECOND_UTTERANCE_ID, '二つ目の質問'), contextA, [])

    const started = controller.receive(
      responseStarted(RESPONSE_ID, { speechSources: [UTTERANCE_ID] }), contextA, [],
    )

    // 応答開始後の本文は保留中表示の結合ではなく、eventの入力源から投影される。
    expect(started).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({
      responseId: RESPONSE_ID,
      userContent: '一つ目の質問',
    })
  })

  test('applies ordered deltas and ignores duplicate or out-of-order deltas', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '質問'), contextA, [])
    controller.receive(responseStarted(RESPONSE_ID), contextA, [])

    const applied = controller.receive(responseDelta(RESPONSE_ID, 1, '逐次'), contextA, [])
    expect(applied).toEqual({ pendingInputAccepted: false, pendingInputResolved: true })

    expect(controller.receive(responseDelta(RESPONSE_ID, 1, '重複'), contextA, []))
      .toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(controller.receive(responseDelta(RESPONSE_ID, 3, '順序外'), contextA, []))
      .toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    controller.receive(responseDelta(RESPONSE_ID, 2, '応答'), contextA, [])

    expect(get(controller).live?.assistantContent).toBe('逐次応答')
  })

  test('ignores a delta for a response that has not started', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '質問'), contextA, [])

    const result = controller.receive(responseDelta(RESPONSE_ID, 1, '早すぎる本文'), contextA, [])

    expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({ responseId: null, assistantContent: '' })
  })

  test('ignores a repeated response_started for the same response', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '質問'), contextA, [])
    controller.receive(responseStarted(RESPONSE_ID), contextA, [])
    controller.receive(responseDelta(RESPONSE_ID, 1, '既に受けた本文'), contextA, [])

    const repeated = controller.receive(responseStarted(RESPONSE_ID), contextA, [])

    expect(repeated).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({ assistantContent: '既に受けた本文' })
  })

  test('ignores a terminal event for a response that is not the live turn', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '質問'), contextA, [])
    controller.receive(responseStarted(RESPONSE_ID), contextA, [])
    controller.receive(responseDelta(RESPONSE_ID, 1, '進行中の回答'), contextA, [])

    const ignored = controller.receive(
      responseTerminal('response_completed', UNRELATED_RESPONSE_ID), contextA, [],
    )

    expect(ignored).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({
      responseId: RESPONSE_ID,
      assistantContent: '進行中の回答',
    })
    expect(get(controller).settled).toEqual([])
  })

  test('ignores events that do not affect the voice history display', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '質問'), contextA, [])

    const result = controller.receive(voiceEvent({
      type: 'user_input_result',
      input_event_id: TEXT_INPUT_ID,
      status: 'accepted',
    }), contextA, [])

    expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({ userContent: '質問' })
  })

  test.each(['response_completed', 'response_cancelled'] as const)(
    'moves a live turn to the settled list on %s and refreshes the saved history',
    (terminal) => {
      const { controller, refreshTurns, refreshCharacter, select } = createHarness()
      select(selectionA(1))
      controller.receive(utteranceFinalized(UTTERANCE_ID, '確定する質問'), contextA, [])
      controller.receive(
        responseStarted(RESPONSE_ID, { historyTurnId: HISTORY_TURN_ID }), contextA, [],
      )
      controller.receive(responseDelta(RESPONSE_ID, 1, '確定する回答'), contextA, [])

      const result = controller.receive(responseTerminal(terminal, RESPONSE_ID), contextA, [])

      expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: true })
      const state = get(controller)
      expect(state.live).toBeNull()
      expect(state.settled).toHaveLength(1)
      expect(state.settled[0]).toMatchObject({
        historyTurnId: HISTORY_TURN_ID,
        responseId: RESPONSE_ID,
        userContent: '確定する質問',
        assistantContent: '確定する回答',
        terminal: terminal === 'response_completed' ? 'completed' : 'cancelled',
        context: { character: 'miori', conversationId: 'conversation-a' },
      })
      expect(refreshTurns).toHaveBeenCalledWith(selectionA(1))
      expect(refreshCharacter).toHaveBeenCalledWith('miori')
    },
  )

  test('clears the live turn on a terminal event without a history turn and still refreshes', () => {
    const { controller, refreshTurns, select } = createHarness()
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, '保存先不明の質問'), contextA, [])
    controller.receive(responseStarted(RESPONSE_ID), contextA, [])

    controller.receive(responseTerminal('response_completed', RESPONSE_ID), contextA, [])

    const state = get(controller)
    expect(state.live).toBeNull()
    expect(state.settled).toEqual([])
    expect(refreshTurns).toHaveBeenCalledWith(selectionA(1))
  })

  test('keeps a settled turn until the saved history confirms it and removes only confirmed turns', async () => {
    const firstRefresh = deferred<void>()
    const secondRefresh = deferred<void>()
    const { controller, refreshTurns, select, saveTurns } = createHarness()
    refreshTurns
      .mockImplementationOnce(() => firstRefresh.promise)
      .mockImplementationOnce(() => secondRefresh.promise)
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, '一つ目の質問'), contextA, [])
    controller.receive(
      responseStarted(RESPONSE_ID, { historyTurnId: HISTORY_TURN_ID }), contextA, [],
    )
    controller.receive(responseDelta(RESPONSE_ID, 1, '一つ目の回答'), contextA, [])
    controller.receive(responseTerminal('response_completed', RESPONSE_ID), contextA, [])

    // 履歴取得の完了を待たずに次の応答を開始できる。
    controller.receive(utteranceFinalized(SECOND_UTTERANCE_ID, '二つ目の質問'), contextA, [])
    controller.receive(responseStarted(SECOND_RESPONSE_ID, {
      historyTurnId: SECOND_HISTORY_TURN_ID,
      speechSources: [SECOND_UTTERANCE_ID],
    }), contextA, [])
    expect(get(controller).live).toMatchObject({
      responseId: SECOND_RESPONSE_ID,
      userContent: '二つ目の質問',
    })
    expect(get(controller).settled).toHaveLength(1)

    controller.receive(responseTerminal('response_completed', SECOND_RESPONSE_ID), contextA, [])
    expect(get(controller).settled).toHaveLength(2)

    saveTurns([savedTurn(HISTORY_TURN_ID)])
    firstRefresh.resolve(undefined)
    await flush()

    const settled = get(controller).settled
    expect(settled).toHaveLength(1)
    expect(settled[0]).toMatchObject({ historyTurnId: SECOND_HISTORY_TURN_ID })
  })

  test('retains the settled turn when the history refresh fails', async () => {
    const { controller, refreshTurns, select } = createHarness()
    refreshTurns.mockRejectedValue(new Error('history unavailable'))
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, '元の質問'), contextA, [])
    controller.receive(
      responseStarted(RESPONSE_ID, { historyTurnId: HISTORY_TURN_ID }), contextA, [],
    )
    controller.receive(responseDelta(RESPONSE_ID, 1, '保持する回答'), contextA, [])
    controller.receive(responseTerminal('response_cancelled', RESPONSE_ID), contextA, [])

    await flush()

    expect(get(controller).settled).toHaveLength(1)
    expect(view(controller, viewA).settled[0]).toMatchObject({
      assistantContent: '保持する回答',
    })
  })

  test('retains the settled turn when the refreshed history does not contain it', async () => {
    const { controller, select, saveTurns } = createHarness()
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, '元の質問'), contextA, [])
    controller.receive(
      responseStarted(RESPONSE_ID, { historyTurnId: HISTORY_TURN_ID }), contextA, [],
    )
    controller.receive(responseDelta(RESPONSE_ID, 1, '保持する回答'), contextA, [])
    controller.receive(responseTerminal('response_completed', RESPONSE_ID), contextA, [])

    saveTurns([])
    await flush()

    expect(get(controller).settled).toHaveLength(1)
  })

  test('does not apply a completed history refresh after the selection moved away', async () => {
    const refresh = deferred<void>()
    const { controller, refreshTurns, select, saveTurns } = createHarness()
    refreshTurns.mockImplementation(() => refresh.promise)
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, '以前の質問'), contextA, [])
    controller.receive(
      responseStarted(RESPONSE_ID, { historyTurnId: HISTORY_TURN_ID }), contextA, [],
    )
    controller.receive(responseDelta(RESPONSE_ID, 1, '以前の回答'), contextA, [])
    controller.receive(responseTerminal('response_completed', RESPONSE_ID), contextA, [])

    select(selectionB(2))
    saveTurns([savedTurn(HISTORY_TURN_ID)])
    refresh.resolve(undefined)
    await flush()

    expect(get(controller).settled).toHaveLength(1)
    expect(view(controller, viewB).settled).toEqual([])
    expect(view(controller, viewA).settled).toHaveLength(1)
  })

  test('does not apply a completed history refresh after the same conversation was reselected', async () => {
    const refresh = deferred<void>()
    const { controller, refreshTurns, select, saveTurns } = createHarness()
    refreshTurns.mockImplementation(() => refresh.promise)
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, '以前の質問'), contextA, [])
    controller.receive(
      responseStarted(RESPONSE_ID, { historyTurnId: HISTORY_TURN_ID }), contextA, [],
    )
    controller.receive(responseTerminal('response_completed', RESPONSE_ID), contextA, [])

    select(selectionB(2))
    select(selectionA(3))
    saveTurns([savedTurn(HISTORY_TURN_ID)])
    refresh.resolve(undefined)
    await flush()

    expect(get(controller).settled).toHaveLength(1)
  })

  test('reconciles a persisted turn after the same conversation is reselected', async () => {
    const refresh = deferred<void>()
    const { controller, refreshTurns, select, saveTurns } = createHarness()
    refreshTurns.mockImplementation(() => refresh.promise)
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, '以前の質問'), contextA, [])
    controller.receive(responseStarted(RESPONSE_ID, { historyTurnId: HISTORY_TURN_ID }), contextA, [])
    controller.receive(responseTerminal('response_completed', RESPONSE_ID), contextA, [])

    select(selectionB(2))
    select(selectionA(3))
    saveTurns([savedTurn(HISTORY_TURN_ID)])
    controller.reconcileSavedTurns(selectionA(3), [savedTurn(HISTORY_TURN_ID)])

    expect(get(controller).settled).toEqual([])
    refresh.resolve(undefined)
    await flush()
    expect(get(controller).settled).toEqual([])
  })

  test('keeps late events for the previous conversation out of the current view', async () => {
    const { controller, refreshTurns, select, saveTurns } = createHarness()
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, 'Aだけの質問'), contextA, [])
    controller.receive(
      responseStarted(RESPONSE_ID, { historyTurnId: HISTORY_TURN_ID }), contextA, [],
    )

    select(selectionB(2))
    const applied = controller.receive(responseDelta(RESPONSE_ID, 1, 'Aだけの回答'), contextA, [])

    expect(applied).toEqual({ pendingInputAccepted: false, pendingInputResolved: true })
    const visibleB = view(controller, viewB)
    expect(visibleB.live).toBeNull()
    expect(visibleB.settled).toEqual([])
    expect(visibleB.failed).toEqual([])
    expect(view(controller, viewA).live).toMatchObject({
      userContent: 'Aだけの質問',
      assistantContent: 'Aだけの回答',
    })

    controller.receive(responseTerminal('response_completed', RESPONSE_ID), contextA, [])
    expect(refreshTurns).toHaveBeenCalledWith(
      expect.objectContaining({ character: 'miori', conversationId: 'conversation-a' }),
    )
    saveTurns([savedTurn(HISTORY_TURN_ID)])
    await flush()

    expect(view(controller, viewB).settled).toEqual([])
    expect(view(controller, viewA).settled).toMatchObject([
      { historyTurnId: HISTORY_TURN_ID, terminal: 'completed' },
    ])
    expect(view(controller, null).live).toBeNull()
  })

  test('separates events when the selected character changes', () => {
    const { controller, select } = createHarness()
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, 'mioriの質問'), contextA, [])
    controller.receive(responseStarted(RESPONSE_ID), contextA, [])

    select({ character: 'akari', conversationId: 'conversation-c', version: 2 })
    controller.receive(responseDelta(RESPONSE_ID, 1, 'mioriの回答'), contextA, [])

    const visibleAkari = view(controller, viewAkari)
    expect(visibleAkari.live).toBeNull()
    expect(visibleAkari.settled).toEqual([])
    expect(visibleAkari.failed).toEqual([])
    expect(view(controller, viewA).live).toMatchObject({
      userContent: 'mioriの質問',
      assistantContent: 'mioriの回答',
    })

    // 別characterのsessionに属するeventは、表示中のturnへ混入しない。
    controller.receive(utteranceFinalized(SECOND_UTTERANCE_ID, 'akariの質問'), contextAkari, [])
    expect(view(controller, viewA).live).toMatchObject({
      userContent: 'mioriの質問',
      assistantContent: 'mioriの回答',
    })
  })

  test.each([
    { phase: 'a pending utterance', started: false },
    { phase: 'a started response', started: true },
  ])(
    'clears the display on privacy skip for $phase and rejects late events for that response',
    ({ started }) => {
      const { controller, refreshTurns, refreshCharacter, select } = createHarness()
      select(selectionA(1))
      controller.receive(utteranceFinalized(UTTERANCE_ID, '保存しない質問'), contextA, [])
      if (started) {
        controller.receive(responseStarted(RESPONSE_ID), contextA, [])
        controller.receive(responseDelta(RESPONSE_ID, 1, '保存しない回答'), contextA, [])
      }

      const result = controller.receive(
        privacySkipped(RESPONSE_ID, [{ input_id: UTTERANCE_ID, source: 'speech' }]),
        contextA,
        [],
      )

      expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: true })
      const state = get(controller)
      expect(state.live).toBeNull()
      expect(state.settled).toEqual([])
      expect(state.failed).toEqual([])
      expect(refreshTurns).toHaveBeenCalledWith(selectionA(1))
      expect(refreshCharacter).toHaveBeenCalledWith('miori')

      // 終端後の遅着した開始・本文は復活させない。
      expect(controller.receive(responseStarted(RESPONSE_ID), contextA, []))
        .toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
      expect(controller.receive(responseDelta(RESPONSE_ID, 2, '遅着本文'), contextA, []))
        .toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
      expect(get(controller).live).toBeNull()
    },
  )

  test('records a privacy-skipped response before it starts and refuses a late start', () => {
    const { controller, refreshTurns, select } = createHarness()
    select(selectionA(1))

    const result = controller.receive(
      privacySkipped(RESPONSE_ID, [{ input_id: TEXT_INPUT_ID, source: 'text' }]),
      contextA,
      [],
    )

    expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(refreshTurns).toHaveBeenCalledWith(selectionA(1))

    expect(controller.receive(responseStarted(RESPONSE_ID, {
      speechSources: [UTTERANCE_ID],
      sourceInputs: [{ input_id: TEXT_INPUT_ID, source: 'text' }],
    }), contextA, [])).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toBeNull()
  })

  test('keeps the live turn when a privacy skip targets another response', () => {
    const { controller, select } = createHarness()
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, '保持する質問'), contextA, [])
    controller.receive(responseStarted(RESPONSE_ID), contextA, [])

    const result = controller.receive(
      privacySkipped(UNRELATED_RESPONSE_ID, [{ input_id: SECOND_UTTERANCE_ID, source: 'speech' }]),
      contextA,
      [],
    )

    expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({ responseId: RESPONSE_ID })
  })

  test('keeps the user utterance and partial answer as a failed turn', async () => {
    const { controller, refreshTurns, refreshCharacter, select, saveTurns } = createHarness()
    select(selectionA(1))
    controller.receive(utteranceFinalized(UTTERANCE_ID, '消してはいけない質問'), contextA, [])
    controller.receive(
      responseStarted(RESPONSE_ID, { historyTurnId: HISTORY_TURN_ID }), contextA, [],
    )
    controller.receive(responseDelta(RESPONSE_ID, 1, '途中までの回答'), contextA, [])

    const result = controller.receive(
      responseTerminal('response_failed', RESPONSE_ID), contextA, [],
    )

    expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: true })
    const state = get(controller)
    expect(state.live).toBeNull()
    expect(state.settled).toEqual([])
    expect(state.failed).toHaveLength(1)
    expect(state.failed[0]).toMatchObject({
      responseId: RESPONSE_ID,
      userContent: '消してはいけない質問',
      assistantContent: '途中までの回答',
      context: { character: 'miori', conversationId: 'conversation-a' },
    })
    expect(refreshTurns).not.toHaveBeenCalled()
    expect(refreshCharacter).not.toHaveBeenCalled()
    expect(view(controller, viewB).failed).toEqual([])

    // 失敗turnは後続の保存履歴反映でも消えない。
    controller.receive(utteranceFinalized(SECOND_UTTERANCE_ID, '次の質問'), contextA, [])
    controller.receive(responseStarted(SECOND_RESPONSE_ID, {
      historyTurnId: SECOND_HISTORY_TURN_ID,
      speechSources: [SECOND_UTTERANCE_ID],
    }), contextA, [])
    controller.receive(responseTerminal('response_completed', SECOND_RESPONSE_ID), contextA, [])
    saveTurns([savedTurn(SECOND_HISTORY_TURN_ID)])
    await flush()
    expect(get(controller).failed).toHaveLength(1)
    expect(get(controller).settled).toEqual([])
  })

  test('clears a pending turn when its utterance is discarded', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '破棄される質問'), contextA, [])

    const result = controller.receive(utteranceDiscarded(UTTERANCE_ID), contextA, [])

    expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: true })
    expect(get(controller).live).toBeNull()
  })

  test('ignores a discarded utterance unrelated to the pending turn', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '残す質問'), contextA, [])

    const result = controller.receive(utteranceDiscarded(SECOND_UTTERANCE_ID), contextA, [])

    expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({
      responseId: null,
      sourceUtteranceIds: [UTTERANCE_ID],
      userContent: '残す質問',
    })
  })

  test('ignores a discarded utterance from another conversation', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '残す質問'), contextA, [])
    const contextB: VoiceSessionContext = { characterId: 'miori', conversationId: 'conversation-b' }

    const result = controller.receive(utteranceDiscarded(UTTERANCE_ID), contextB, [])

    expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({ userContent: '残す質問' })
  })

  test('keeps a response-bound live turn when the utterance is discarded', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '応答済みの質問'), contextA, [])
    controller.receive(responseStarted(RESPONSE_ID), contextA, [])

    const result = controller.receive(utteranceDiscarded(UTTERANCE_ID), contextA, [])

    // 破棄は待機状態の終了として扱い、応答に紐付いたturnの表示は維持する。
    expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: true })
    expect(get(controller).live).toMatchObject({ responseId: RESPONSE_ID })
  })

  test('leaves the pending turn untouched by a recoverable error event', () => {
    const { controller } = createHarness()
    controller.receive(utteranceFinalized(UTTERANCE_ID, '残る質問'), contextA, [])

    const result = controller.receive(voiceEvent({
      type: 'error',
      utterance_id: UTTERANCE_ID,
      classification: 'recoverable',
      error_code: 'stt_inference_timeout',
      user_state: 'listening',
    }), contextA, [])

    expect(result).toEqual({ pendingInputAccepted: false, pendingInputResolved: false })
    expect(get(controller).live).toMatchObject({ userContent: '残る質問' })
  })
})
