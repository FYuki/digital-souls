import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte'
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import App from './App.svelte'

const MEMORY_ID = '00000000-0000-4000-8000-000000000012'
const RECORD_ID = '10000000-0000-4000-8000-000000000012'

describe('記憶管理画面', () => {
  let personaPatchRejected = false
  let temporaryPatchBody: unknown = null
  let temporaryDeleteCalled = false

  beforeEach(() => {
    localStorage.clear()
    personaPatchRejected = false
    temporaryPatchBody = null
    temporaryDeleteCalled = false
    vi.stubGlobal('WebSocket', class {
      close() {}
    })
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith(`/persona-memories/${MEMORY_ID}`) && init?.method === 'PATCH') {
        personaPatchRejected = true
        return new Response(JSON.stringify({ reason_code: 'DENY_SENSITIVE' }), { status: 422 })
      }
      if (url.endsWith(`/temporary-records/temporary:recipe/${RECORD_ID}`) && init?.method === 'PATCH') {
        temporaryPatchBody = JSON.parse(String(init.body))
        return new Response(JSON.stringify({
          id: RECORD_ID,
          character_id: 'miori',
          provider_id: 'temporary:recipe',
          source_ref: 'recipe-12',
          record_type: 'MEAL_PLAN',
          structured_value: '{"name":"シチュー"}',
          effective_at: '2026-08-24T12:30:00.000000Z',
          updated_at: '2026-08-23T00:00:00.000000Z',
        }), { status: 200 })
      }
      if (url.endsWith(`/temporary-records/temporary:recipe/${RECORD_ID}`) && init?.method === 'DELETE') {
        temporaryDeleteCalled = true
        return new Response(null, { status: 204 })
      }
      if (url.includes('/persona-memories')) {
        return new Response(JSON.stringify([{
          id: MEMORY_ID,
          character_id: 'miori',
          provider_id: 'core',
          memory_kind: 'SEMANTIC',
          memory_type: 'USER_PREFERENCE',
          normalized_text: 'ユーザーは紅茶を好む',
          effective_at: '2026-08-21T00:00:00.000000Z',
          status: 'ACTIVE',
          content_version: 1,
          index_pending: true,
        }]), { status: 200 })
      }
      if (url.includes('/temporary-records/temporary:recipe')) {
        return new Response(JSON.stringify([{
          id: RECORD_ID,
          character_id: 'miori',
          provider_id: 'temporary:recipe',
          source_ref: 'recipe-12',
          record_type: 'RECIPE',
          structured_value: '{"name":"カレー"}',
          effective_at: '2026-08-20T00:00:00.000000Z',
          updated_at: '2026-08-22T00:00:00.000000Z',
        }]), { status: 200 })
      }
      if (url.includes('/temporary-records/temporary:agriculture')) {
        return new Response('[]', { status: 200 })
      }
      return new Response('[]', { status: 200 })
    }))
  })

  afterEach(() => vi.unstubAllGlobals())

  test('チャットとは別の入口から人格記憶と暫定記録を別groupで表示する', async () => {
    render(App)

    await fireEvent.click(screen.getByRole('button', { name: '記憶管理' }))

    expect(await screen.findByRole('heading', { name: '人格記憶' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: '暫定記録' })).toBeTruthy()
    expect(screen.getByText('ユーザーは紅茶を好む')).toBeTruthy()
    expect(screen.getByText('{"name":"カレー"}')).toBeTruthy()
    expect(screen.getByText('USER_PREFERENCE')).toBeTruthy()
    expect(screen.getByText(/2026-08-20/)).toBeTruthy()
    expect(screen.getByText('ACTIVE')).toBeTruthy()
    expect(screen.getByText('RECIPE')).toBeTruthy()
    expect(screen.getByText(/2026-08-21/)).toBeTruthy()
    expect(screen.getByText(/2026-08-22/)).toBeTruthy()
    expect(screen.getByText('index反映待ち')).toBeTruthy()
  })

  test('候補確認・保存通知・pending confirmationを表示しない', async () => {
    render(App)
    await fireEvent.click(screen.getByRole('button', { name: '記憶管理' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: '人格記憶' })).toBeTruthy())

    expect(screen.queryByText('候補を確認')).toBeNull()
    expect(screen.queryByText('保存しました')).toBeNull()
    expect(screen.queryByText('確認待ち')).toBeNull()
  })

  test('訂正拒否理由を表示し再編集と明示削除の選択を残す', async () => {
    render(App)
    await fireEvent.click(screen.getByRole('button', { name: '記憶管理' }))
    await fireEvent.click(await screen.findByRole('button', { name: `詳細・訂正 ${MEMORY_ID}` }))
    await fireEvent.click(screen.getByRole('button', { name: '訂正を保存' }))

    expect(personaPatchRejected).toBe(true)
    expect(await screen.findByText('DENY_SENSITIVE')).toBeTruthy()
    expect(screen.getByRole('button', { name: '再編集' })).toBeTruthy()
    expect(screen.getByRole('button', { name: `削除 ${MEMORY_ID}` })).toBeTruthy()
  })

  test('暫定記録を訂正し明示削除できる', async () => {
    render(App)
    await fireEvent.click(screen.getByRole('button', { name: '記憶管理' }))
    await fireEvent.click(await screen.findByRole('button', { name: `詳細・訂正 ${RECORD_ID}` }))
    await fireEvent.input(screen.getByRole('textbox', { name: '種別' }), {
      target: { value: 'MEAL_PLAN' },
    })
    await fireEvent.input(screen.getByRole('textbox', { name: '構造化値' }), {
      target: { value: '{"name":"シチュー"}' },
    })
    await fireEvent.input(screen.getByRole('textbox', { name: '有効日時' }), {
      target: { value: '2026-08-24T12:30:00.000000Z' },
    })
    await fireEvent.click(screen.getByRole('button', { name: '訂正を保存' }))

    await waitFor(() => expect(temporaryPatchBody).toEqual({
      record_type: 'MEAL_PLAN',
      structured_value: JSON.stringify({ name: 'シチュー' }),
      effective_at: '2026-08-24T12:30:00.000000Z',
    }))
    expect((screen.getByRole('textbox', { name: '種別' }) as HTMLInputElement).value).toBe('MEAL_PLAN')
    expect((screen.getByRole('textbox', { name: '構造化値' }) as HTMLTextAreaElement).value).toBe(JSON.stringify({ name: 'シチュー' }))
    expect((screen.getByRole('textbox', { name: '有効日時' }) as HTMLInputElement).value).toBe('2026-08-24T12:30:00.000000Z')
    expect(await screen.findByText('MEAL_PLAN')).toBeTruthy()
    expect(await screen.findByText('{"name":"シチュー"}')).toBeTruthy()

    expect(screen.getByText(/2026-08-24/)).toBeTruthy()

    await fireEvent.click(screen.getByRole('button', { name: `削除 ${RECORD_ID}` }))
    await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: '完全に削除' }))

    await waitFor(() => expect(temporaryDeleteCalled).toBe(true))
    await waitFor(() => expect(screen.queryByText('{"name":"シチュー"}')).toBeNull())
  })
})
