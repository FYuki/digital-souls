import { get } from 'svelte/store'
import { describe, expect, test, vi } from 'vitest'

import type { CharacterCatalogEntry } from '../characters/types'
import type { Conversation } from '../conversations/types'
import type { UiSettings } from '../ui-settings/types'
import {
  collapsedThreads,
  createSidebarController,
  orderThreadsForCharacter,
  selectCharacterGroups,
  type CharacterThreadGroup,
  type SidebarGateway,
  type SidebarState,
} from './controller'

const catalog: CharacterCatalogEntry[] = [
  {
    character_id: 'miori',
    display_name: '光織',
    standing_image: { status: 'available', url: '/miori.png' },
  },
  {
    character_id: 'akira',
    display_name: '晶',
    standing_image: { status: 'missing', url: null },
  },
]

const conversation = (
  conversationId: string,
  characterId = 'miori',
  updatedAt = '2026-09-01T00:00:00Z',
  archived = false,
): Conversation => ({
  character_id: characterId,
  conversation_id: conversationId,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: updatedAt,
  archived_at: archived ? '2026-09-02T00:00:00Z' : null,
  title: conversationId,
})

const ids = [
  '10000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000002',
  '10000000-0000-4000-8000-000000000003',
  '10000000-0000-4000-8000-000000000004',
  '10000000-0000-4000-8000-000000000005',
  '10000000-0000-4000-8000-000000000006',
  '10000000-0000-4000-8000-000000000007',
]

const settings = (overrides: Partial<UiSettings> = {}): UiSettings => ({
  user_id: 'local',
  desktop_portrait_layout: 'right',
  desktop_history_height_percent: 75,
  compact_history_height_percent: 75,
  characters: [
    { character_id: 'miori', visible: true, pinned: false, pin_order: null },
    { character_id: 'akira', visible: true, pinned: false, pin_order: null },
  ],
  thread_pins: [],
  ...overrides,
})

const state = (overrides: Partial<SidebarState> = {}): SidebarState => ({
  catalog,
  settings: settings(),
  activeByCharacter: {},
  archivedByCharacter: {},
  archivedLoaded: [],
  showingArchived: false,
  expandedGroups: [],
  pending: false,
  initialized: true,
  error: null,
  ...overrides,
})

const gateway = (overrides: Partial<SidebarGateway> = {}): SidebarGateway => ({
  listCatalog: vi.fn(async () => catalog),
  rescanCatalog: vi.fn(async () => catalog),
  getSettings: vi.fn(async () => settings()),
  setCharacterVisibility: vi.fn(async () => settings()),
  setCharacterPinned: vi.fn(async () => settings()),
  setThreadPinned: vi.fn(async () => settings()),
  listActive: vi.fn(async () => []),
  listArchived: vi.fn(async () => []),
  create: vi.fn(async (characterId) => conversation(ids[0], characterId)),
  rename: vi.fn(async (characterId, conversationId, title) => ({
    ...conversation(conversationId, characterId),
    title,
  })),
  archive: vi.fn(async (characterId, conversationId) => (
    conversation(conversationId, characterId, '2026-09-02T00:00:00Z', true)
  )),
  unarchive: vi.fn(async (characterId, conversationId) => (
    conversation(conversationId, characterId)
  )),
  hardDelete: vi.fn(async () => undefined),
  ...overrides,
})

describe('sidebar controller selectors', () => {
  test('キャラクターをピン順、その後は最新活動順で並べ、0件も残す', () => {
    const value = state({
      settings: settings({
        characters: [
          { character_id: 'miori', visible: true, pinned: false, pin_order: null },
          { character_id: 'akira', visible: true, pinned: true, pin_order: 1 },
        ],
      }),
      activeByCharacter: {
        miori: [conversation(ids[0])],
        akira: [],
      },
    })

    const groups = selectCharacterGroups(value)

    expect(groups.map((group) => group.character.character_id)).toEqual(['akira', 'miori'])
    expect(groups[0].conversations).toEqual([])
  })

  test('スレッドをピン留め優先、それぞれ更新日時降順で並べる', () => {
    const value = settings({
      thread_pins: [
        { character_id: 'miori', conversation_id: ids[0] },
        { character_id: 'miori', conversation_id: ids[2] },
      ],
    })
    const threads = [
      conversation(ids[0], 'miori', '2026-09-01T00:00:00Z'),
      conversation(ids[1], 'miori', '2026-09-05T00:00:00Z'),
      conversation(ids[2], 'miori', '2026-09-03T00:00:00Z'),
    ]

    expect(orderThreadsForCharacter(threads, value).map((item) => item.conversation_id))
      .toEqual([ids[2], ids[0], ids[1]])
  })

  test('通常は合計5件、ピンが6件以上なら全ピンだけを表示する', () => {
    const conversations = ids.map((id, index) => (
      conversation(id, 'miori', `2026-09-0${index + 1}T00:00:00Z`)
    ))
    const baseGroup: CharacterThreadGroup = {
      character: catalog[0],
      conversations,
      pinned: false,
      pinOrder: null,
      expanded: false,
    }
    const threePins = settings({
      thread_pins: ids.slice(0, 3).map((conversationId) => ({
        character_id: 'miori', conversation_id: conversationId,
      })),
    })
    const sixPins = settings({
      thread_pins: ids.slice(0, 6).map((conversationId) => ({
        character_id: 'miori', conversation_id: conversationId,
      })),
    })

    expect(collapsedThreads(baseGroup, threePins)).toEqual({
      visible: conversations.slice(0, 5),
      hiddenCount: 2,
    })
    expect(collapsedThreads(baseGroup, sixPins)).toEqual({
      visible: conversations.slice(0, 6),
      hiddenCount: 1,
    })
  })
})

describe('sidebar controller operations', () => {
  test('初期化時に可視キャラクターだけのactive一覧を取得する', async () => {
    const listActive = vi.fn(async (characterId: string) => [
      conversation(ids[0], characterId),
    ])
    const controller = createSidebarController(gateway({ listActive }), '取得失敗')

    await controller.initialize()

    expect(listActive).toHaveBeenCalledTimes(2)
    expect(get(controller).initialized).toBe(true)
    expect(Object.keys(get(controller).activeByCharacter)).toEqual(['miori', 'akira'])
  })

  test('非表示後の再追加で保存済みスレッドを再取得する', async () => {
    let visible = true
    const listActive = vi.fn(async (characterId: string) => [
      conversation(ids[0], characterId),
    ])
    const setCharacterVisibility = vi.fn(async (characterId: string, next: boolean) => {
      visible = next
      return settings({
        characters: [
          { character_id: 'miori', visible, pinned: false, pin_order: null },
          { character_id: 'akira', visible: true, pinned: false, pin_order: null },
        ],
      })
    })
    const controller = createSidebarController(gateway({
      listActive,
      setCharacterVisibility,
    }), '取得失敗')
    await controller.initialize()

    await controller.hideCharacter('miori')
    expect(selectCharacterGroups(get(controller)).map((group) => group.character.character_id))
      .toEqual(['akira'])
    await controller.addCharacter('miori')

    expect(listActive).toHaveBeenCalledWith('miori')
    expect(get(controller).activeByCharacter.miori[0].conversation_id).toBe(ids[0])
  })

  test('設定更新失敗時は表示状態を変更しない', async () => {
    const controller = createSidebarController(gateway({
      setCharacterVisibility: vi.fn(async () => { throw new Error('failed') }),
    }), '取得失敗')
    await controller.initialize()
    const before = get(controller).settings

    await controller.hideCharacter('miori')

    expect(get(controller).settings).toEqual(before)
    expect(get(controller).error).toBe('取得失敗')
  })

  test('スレッドのピン留め結果をサーバーの正規設定から反映する', async () => {
    const pinnedSettings = settings({
      thread_pins: [{ character_id: 'miori', conversation_id: ids[0] }],
    })
    const setThreadPinned = vi.fn(async () => pinnedSettings)
    const controller = createSidebarController(gateway({ setThreadPinned }), '取得失敗')
    await controller.initialize()

    await controller.setThreadPinned('miori', ids[0], true)

    expect(setThreadPinned).toHaveBeenCalledWith('miori', ids[0], true)
    expect(get(controller).settings).toEqual(pinnedSettings)
  })

  test('アーカイブと物理削除を再取得なしで一覧へ反映する', async () => {
    const controller = createSidebarController(gateway({
      getSettings: vi.fn(async () => settings({
        thread_pins: [{ character_id: 'miori', conversation_id: ids[0] }],
      })),
      listActive: vi.fn(async (characterId) => (
        characterId === 'miori' ? [conversation(ids[0])] : []
      )),
    }), '取得失敗')
    await controller.initialize()

    expect(await controller.archiveConversation('miori', ids[0])).toBe(true)
    expect(get(controller).activeByCharacter.miori).toEqual([])
    expect(get(controller).archivedByCharacter.miori[0].archived_at).not.toBeNull()

    expect(await controller.hardDeleteConversation('miori', ids[0])).toBe(true)
    expect(get(controller).archivedByCharacter.miori).toEqual([])
    expect(get(controller).settings?.thread_pins).toEqual([])
  })
})
