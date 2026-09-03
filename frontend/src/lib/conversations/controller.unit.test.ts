import { get } from 'svelte/store'
import { beforeEach, describe, expect, test, vi } from 'vitest'

import type { ConversationSessionManager } from '../conversation-session'
import {
  createConversationController,
  type ConversationControllerState,
  type ConversationGateway,
} from './controller'
import type { Conversation, ConversationTurn } from './types'

const FIRST_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
const SECOND_ID = '6ad9a610-02cc-4a41-b02e-503826f7292b'

const conversation = (
  conversationId: string,
  updatedAt: string,
  archivedAt: string | null = null,
  character = 'miori',
): Conversation => ({
  character_id: character,
  conversation_id: conversationId,
  created_at: '2026-08-01T10:00:00+00:00',
  updated_at: updatedAt,
  archived_at: archivedAt,
  title: conversationId,
})

const turn = (turnId: string): ConversationTurn => ({
  kind: 'content',
  turn_id: turnId,
  user_content: `質問-${turnId}`,
  assistant_content: `回答-${turnId}`,
})

const privacySkippedTurn = (turnId: string): ConversationTurn => ({
  kind: 'privacy_skipped',
  turn_id: turnId,
  reason_code: 'history-storage-denied',
  sanitizer_version: '1',
  policy_version: '1',
})

const deferred = <T>() => {
  let resolve: (value: T) => void = () => { throw new Error('resolver is not initialized') }
  const promise = new Promise<T>((next) => { resolve = next })
  return { promise, resolve }
}

const createGateway = (): ConversationGateway => ({
  listActive: vi.fn(async () => []),
  listArchived: vi.fn(async () => []),
  listTurns: vi.fn(async () => []),
  create: vi.fn(async () => conversation(FIRST_ID, '2026-08-01T12:00:00+00:00')),
  archive: vi.fn(async () => conversation(FIRST_ID, '2026-08-01T12:00:00+00:00', '2026-08-01T13:00:00+00:00')),
  unarchive: vi.fn(async () => conversation(FIRST_ID, '2026-08-01T12:00:00+00:00')),
  hardDelete: vi.fn(async () => undefined),
})

const createSessions = (): ConversationSessionManager => ({
  getSelectedConversationId: vi.fn(() => null),
  selectConversation: vi.fn(),
  clearConversation: vi.fn(),
})

describe('ConversationController', () => {
  let gateway: ConversationGateway
  let sessions: ConversationSessionManager

  beforeEach(() => {
    gateway = createGateway()
    sessions = createSessions()
  })

  test('should ignore an active-list response from the previous character', async () => {
    const mioriResponse = deferred<Conversation[]>()
    vi.mocked(gateway.listActive)
      .mockImplementationOnce(() => mioriResponse.promise)
      .mockResolvedValueOnce([conversation(SECOND_ID, '2026-08-01T12:00:00+00:00', null, 'akari')])
    const controller = createConversationController('miori', 'error', gateway, sessions)

    const loadingMiori = controller.loadCharacter('miori')
    await controller.loadCharacter('akari')
    mioriResponse.resolve([conversation(FIRST_ID, '2026-08-01T13:00:00+00:00')])
    await loadingMiori

    expect(get(controller).character).toBe('akari')
    expect(get(controller).active.map((item) => item.conversation_id)).toEqual([SECOND_ID])
  })

  test('should merge turns received while history is loading and ignore a later stale history', async () => {
    const firstHistory = deferred<ConversationTurn[]>()
    vi.mocked(gateway.listTurns)
      .mockImplementationOnce(() => firstHistory.promise)
      .mockResolvedValueOnce([turn('second-history')])
    const controller = createConversationController('miori', 'error', gateway, sessions)

    const loadingFirst = controller.selectConversation(FIRST_ID)
    const firstContext = controller.selectedContext()
    if (firstContext === null) throw new Error('selected context is required')
    controller.appendTurn(firstContext, turn('received'))
    await controller.selectConversation(SECOND_ID)
    firstHistory.resolve([turn('first-history'), turn('received')])
    await loadingFirst

    expect(get(controller).selectedConversationId).toBe(SECOND_ID)
    expect(get(controller).turns.map((item) => item.turn_id)).toEqual(['second-history'])
  })

  test('should isolate internal state from mutations to published conversation and turn snapshots', async () => {
    const active = conversation(FIRST_ID, '2026-08-01T14:00:00+00:00')
    const archived = conversation(
      SECOND_ID,
      '2026-08-01T12:00:00+00:00',
      '2026-08-01T13:00:00+00:00',
    )
    const content = turn('content-turn')
    const privacySkipped = privacySkippedTurn('privacy-skipped-turn')
    vi.mocked(gateway.listActive).mockResolvedValue([active])
    vi.mocked(gateway.listArchived).mockResolvedValue([archived])
    vi.mocked(gateway.listTurns).mockResolvedValue([content, privacySkipped])
    const controller = createConversationController('miori', 'error', gateway, sessions)
    await controller.loadCharacter('miori')
    await controller.selectConversation(FIRST_ID)
    await controller.showArchived()

    const publishedStates: ConversationControllerState[] = []
    const unsubscribe = controller.subscribe((state) => publishedStates.push(state))
    const published = publishedStates.at(-1)
    if (published === undefined) throw new Error('published state is required')
    published.active[0].updated_at = 'mutated-active'
    published.archived[0].archived_at = 'mutated-archived'
    const publishedContent = published.turns[0]
    const publishedPrivacySkipped = published.turns[1]
    if (publishedContent.kind !== 'content' || publishedPrivacySkipped.kind !== 'privacy_skipped') {
      throw new Error('expected both turn variants')
    }
    publishedContent.user_content = 'mutated-content'
    publishedPrivacySkipped.reason_code = 'mutated-reason'
    published.active.push(conversation(SECOND_ID, '2026-08-01T15:00:00+00:00'))
    published.archived.splice(0, 1)
    published.turns.splice(0, 2)
    unsubscribe()

    const current = get(controller)
    expect(current.active).toEqual([active])
    expect(current.archived).toEqual([archived])
    expect(current.turns).toEqual([content, privacySkipped])
  })

  test('should apply archive and unarchive immediately while preserving active-list order', async () => {
    const newest = conversation(SECOND_ID, '2026-08-01T14:00:00+00:00')
    const older = conversation(FIRST_ID, '2026-08-01T12:00:00+00:00')
    vi.mocked(gateway.listActive).mockResolvedValue([newest, older])
    vi.mocked(gateway.archive).mockResolvedValue({ ...older, archived_at: '2026-08-01T15:00:00+00:00' })
    vi.mocked(gateway.unarchive).mockResolvedValue(older)
    const controller = createConversationController('miori', 'error', gateway, sessions)
    await controller.loadCharacter('miori')

    await controller.archiveConversation(FIRST_ID)
    expect(get(controller).active.map((item) => item.conversation_id)).toEqual([SECOND_ID])
    expect(get(controller).archived.map((item) => item.conversation_id)).toEqual([FIRST_ID])

    await controller.unarchiveConversation(FIRST_ID)
    expect(get(controller).archived).toEqual([])
    expect(get(controller).active.map((item) => item.conversation_id)).toEqual([SECOND_ID, FIRST_ID])
  })

  test('should not reintroduce an archived conversation from an older archived-list response', async () => {
    const archivedResponse = deferred<Conversation[]>()
    const archived = conversation(FIRST_ID, '2026-08-01T12:00:00+00:00', '2026-08-01T13:00:00+00:00')
    vi.mocked(gateway.listArchived).mockImplementation(() => archivedResponse.promise)
    vi.mocked(gateway.unarchive).mockResolvedValue({ ...archived, archived_at: null })
    const controller = createConversationController('miori', 'error', gateway, sessions)

    const loadingArchived = controller.showArchived()
    await controller.unarchiveConversation(FIRST_ID)
    archivedResponse.resolve([archived])
    await loadingArchived

    expect(get(controller).archived).toEqual([])
    expect(get(controller).active.map((item) => item.conversation_id)).toEqual([FIRST_ID])
  })

  test('should not select or persist a conversation created for the previous character', async () => {
    const createResponse = deferred<Conversation>()
    vi.mocked(gateway.create).mockImplementation(() => createResponse.promise)
    vi.mocked(gateway.listActive).mockResolvedValue([
      conversation(SECOND_ID, '2026-08-01T14:00:00+00:00', null, 'akari'),
    ])
    const controller = createConversationController('miori', 'error', gateway, sessions)

    const creating = controller.createConversation()
    await controller.loadCharacter('akari')
    createResponse.resolve(conversation(FIRST_ID, '2026-08-01T15:00:00+00:00'))
    await creating

    expect(get(controller).active.map((item) => item.conversation_id)).toEqual([SECOND_ID])
    expect(get(controller).selectedConversationId).toBeNull()
    expect(sessions.selectConversation).not.toHaveBeenCalled()
    expect(gateway.listTurns).not.toHaveBeenCalled()
  })

  test('should not apply an archive response from the previous character', async () => {
    const archiveResponse = deferred<Conversation>()
    vi.mocked(gateway.archive).mockImplementation(() => archiveResponse.promise)
    vi.mocked(gateway.listActive).mockResolvedValue([
      conversation(SECOND_ID, '2026-08-01T14:00:00+00:00', null, 'akari'),
    ])
    const controller = createConversationController('miori', 'error', gateway, sessions)

    const archiving = controller.archiveConversation(FIRST_ID)
    await controller.loadCharacter('akari')
    archiveResponse.resolve(conversation(
      FIRST_ID,
      '2026-08-01T15:00:00+00:00',
      '2026-08-01T16:00:00+00:00',
    ))
    await archiving

    expect(get(controller).active.map((item) => item.conversation_id)).toEqual([SECOND_ID])
    expect(get(controller).archived).toEqual([])
    expect(sessions.clearConversation).not.toHaveBeenCalled()
  })

  test('should not apply an unarchive response from the previous character', async () => {
    const unarchiveResponse = deferred<Conversation>()
    vi.mocked(gateway.unarchive).mockImplementation(() => unarchiveResponse.promise)
    vi.mocked(gateway.listActive).mockResolvedValue([
      conversation(SECOND_ID, '2026-08-01T14:00:00+00:00', null, 'akari'),
    ])
    const controller = createConversationController('miori', 'error', gateway, sessions)

    const unarchiving = controller.unarchiveConversation(FIRST_ID)
    await controller.loadCharacter('akari')
    unarchiveResponse.resolve(conversation(FIRST_ID, '2026-08-01T15:00:00+00:00'))
    await unarchiving

    expect(get(controller).active.map((item) => item.conversation_id)).toEqual([SECOND_ID])
    expect(get(controller).archived).toEqual([])
  })

  test('should clear only the previous character session after its hard delete completes', async () => {
    const deleteResponse = deferred<void>()
    vi.mocked(gateway.hardDelete).mockImplementation(() => deleteResponse.promise)
    vi.mocked(gateway.listActive).mockResolvedValue([
      conversation(SECOND_ID, '2026-08-01T14:00:00+00:00', null, 'akari'),
    ])
    vi.mocked(sessions.getSelectedConversationId).mockImplementation((character) => (
      character === 'akari' ? SECOND_ID : FIRST_ID
    ))
    const controller = createConversationController('miori', 'error', gateway, sessions)

    controller.requestHardDelete(FIRST_ID)
    const deleting = controller.confirmHardDelete()
    await controller.loadCharacter('akari')
    deleteResponse.resolve(undefined)
    await deleting

    expect(get(controller).active.map((item) => item.conversation_id)).toEqual([SECOND_ID])
    expect(get(controller).selectedConversationId).toBe(SECOND_ID)
    expect(get(controller).deleteCandidate).toBeNull()
    expect(sessions.clearConversation).toHaveBeenCalledOnce()
    expect(sessions.clearConversation).toHaveBeenCalledWith('miori', FIRST_ID)
    expect(sessions.clearConversation).not.toHaveBeenCalledWith('akari', SECOND_ID)
  })

  test('should remove a hard-deleted conversation from lists, selection, and persisted session', async () => {
    const active = conversation(FIRST_ID, '2026-08-01T12:00:00+00:00')
    vi.mocked(gateway.listActive).mockResolvedValue([active])
    const controller = createConversationController('miori', 'error', gateway, sessions)
    await controller.loadCharacter('miori')
    await controller.selectConversation(FIRST_ID)

    controller.requestHardDelete(FIRST_ID)
    await controller.confirmHardDelete()

    expect(get(controller).active).toEqual([])
    expect(get(controller).selectedConversationId).toBeNull()
    expect(get(controller).deleteCandidate).toBeNull()
    expect(sessions.clearConversation).toHaveBeenCalledWith('miori', FIRST_ID)
  })

  test('should retain state and report an error when a lifecycle request fails', async () => {
    const active = conversation(FIRST_ID, '2026-08-01T12:00:00+00:00')
    vi.mocked(gateway.listActive).mockResolvedValue([active])
    vi.mocked(gateway.archive).mockRejectedValue(new Error('failed'))
    const controller = createConversationController('miori', 'error', gateway, sessions)
    await controller.loadCharacter('miori')

    await controller.archiveConversation(FIRST_ID)

    expect(get(controller).active).toEqual([active])
    expect(get(controller).pending).toBe(false)
    expect(get(controller).error).toBe('error')
  })
})
