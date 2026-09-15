import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import SemanticMemoryManagement from './lib/SemanticMemoryManagement.svelte'
import type { SemanticMemory } from './lib/memory/semantic-client'

function record(id: string, editable: boolean): SemanticMemory {
  return {
    id, character_id: 'miori', formation_type: editable ? 'DIRECT_EXTRACTION' : 'EXPERIENCE_DERIVED',
    status: 'ACTIVE', content_version: 1, confidence: .9, created_at: '2026-09-15T00:00:00Z',
    updated_at: '2026-09-15T00:00:00Z', can_correct: editable, reassessment_pending: false,
    proposition: { subject: 'ユーザー', predicate: '紅茶の好み', value: '好き',
      content: 'ユーザーの紅茶の好みは好き', self_report: editable, mutability: 'CHANGEABLE',
      valid_from: null, valid_until: null },
    sources: [{ kind: editable ? 'CONVERSATION' : 'EPISODE', source_id: 'source',
      revision: 2, conversation_id: editable ? 'conversation' : null, span: null }],
    stamp: { policy_version: 'p1', model_id: 'privacy', prompt_version: 'p1', extraction: null },
    versions: [], relations: [],
  }
}
let records: SemanticMemory[]
let requests: { method: string; body: Record<string, unknown> }[]
let failure: number
beforeEach(() => {
  records = [record('self', true), record('derived', false)]
  requests = []
  failure = 0
  vi.stubGlobal('fetch', vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (!init?.method) return new Response(JSON.stringify(records))
    const body = JSON.parse(String(init.body))
    requests.push({ method: init.method, body })
    if (failure) return new Response('{}', { status: failure })
    if (init.method === 'PATCH') {
      records = records.map(r => r.id === 'self' ? { ...r, id: 'corrected', proposition: { ...r.proposition!,
        value: String(body.value), content: 'ユーザーの紅茶の好みは苦手' } } : r)
      return new Response(JSON.stringify(records[0]))
    }
    records = records.map(r => r.id === 'derived' ? { ...r, status: 'DELETED', proposition: null } : r)
    return new Response(null, { status: 204 })
  }))
})
afterEach(() => vi.unstubAllGlobals())

test('自己申告だけに訂正を表示し、出典会話と経験へ移動できる', async () => {
  const open = vi.fn()
  render(SemanticMemoryManagement, { character: 'miori', onOpenConversation: open })
  const self = await screen.findByRole('article', { name: '意味記憶 self' })
  const derived = screen.getByRole('article', { name: '意味記憶 derived' })
  expect(within(self).getByRole('button', { name: '自己申告を訂正' })).toBeTruthy()
  expect(within(derived).queryByRole('button', { name: '自己申告を訂正' })).toBeNull()
  await fireEvent.click(within(self).getByRole('button', { name: '出典の会話を開く', hidden: true }))
  expect(open).toHaveBeenCalledWith('conversation')
  expect(within(derived).getByRole('link', { name: '根拠の経験を開く', hidden: true }).getAttribute('href')).toBe('#episodic-source')
})

test('訂正する値と版だけを送り、自己申告や所有者は変更しない', async () => {
  render(SemanticMemoryManagement, { character: 'miori' })
  await fireEvent.click(await screen.findByRole('button', { name: '自己申告を訂正' }))
  const form = screen.getByRole('form', { name: '意味記憶の訂正' })
  await fireEvent.input(within(form).getByRole('textbox'), { target: { value: '苦手' } })
  await fireEvent.submit(form)
  await waitFor(() => expect(requests).toHaveLength(1))
  expect(requests[0].body).toEqual({ expected_version: 1, value: '苦手', idempotency_key: expect.any(String) })
  await waitFor(() => expect(screen.queryByRole('form')).toBeNull())
  expect(await screen.findByText('ユーザーの紅茶の好みは苦手')).toBeTruthy()
  await waitFor(() => expect(document.activeElement).toBe(
    within(screen.getByRole('article', { name: '意味記憶 corrected' })).getByRole('button', { name: '自己申告を訂正' })))
})

test('一般化も削除でき、成功後は本文を画面に残さない', async () => {
  render(SemanticMemoryManagement, { character: 'miori' })
  const derived = await screen.findByRole('article', { name: '意味記憶 derived' })
  await fireEvent.click(within(derived).getByRole('button', { name: '削除' }))
  await fireEvent.click(within(derived).getByRole('button', { name: '完全に削除' }))
  await waitFor(() => expect(within(screen.getByRole('article', { name: '意味記憶 derived' })).queryByText('ユーザーの紅茶の好みは好き')).toBeNull())
  expect(requests[0]).toEqual({ method: 'DELETE', body: { expected_version: 1 } })
  await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('article', { name: '意味記憶 derived' })))
})

test('訂正の再送キーを維持し、版競合では再読込を案内する', async () => {
  failure = 409
  render(SemanticMemoryManagement, { character: 'miori' })
  await fireEvent.click(await screen.findByRole('button', { name: '自己申告を訂正' }))
  const form = screen.getByRole('form', { name: '意味記憶の訂正' })
  await fireEvent.submit(form)
  expect((await screen.findByRole('alert')).textContent).toContain('再読み込み')
  await fireEvent.submit(form)
  expect(requests[0].body.idempotency_key).toBe(requests[1].body.idempotency_key)
})
