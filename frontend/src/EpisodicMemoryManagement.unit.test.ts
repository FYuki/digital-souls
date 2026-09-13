import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import EpisodicMemoryManagement from './lib/EpisodicMemoryManagement.svelte'
import MemoryManagement from './lib/MemoryManagement.svelte'
import { emptyTimeParts, type EpisodicMemory, type FiveW } from './lib/memory/episodic-client'

const FACT = '00000000-0000-4000-8000-000000000012'
const EPISODE = '00000000-0000-4000-8000-000000000013'
const value: FiveW = {
  who: [{ name: 'ユーザー', role: 'ACTOR', entity_id: 'speaker:user' }],
  what: { predicate: '食べた', object: 'うどん', polarity: 'AFFIRMED', actuality: 'OCCURRED' },
  when: { parts: { ...emptyTimeParts(), year: 2026, month: 9 }, end: null,
    range_kind: 'POINT', timezone: 'Asia/Tokyo', reference_at: '2026-09-13T00:00:00Z' },
  where: null, why: null, context: 'HYPOTHETICAL',
}
function record(id: string, kind: EpisodicMemory['kind']): EpisodicMemory {
  return { id, kind, character_id: 'miori', conversation_id: EPISODE,
    content_version: 1, status: 'ACTIVE', stored_status: 'ACTIVE', five_w: structuredClone(value),
    normalized_text: kind === 'FACT' ? '仮定：ユーザーがうどんを食べた / 2026年9月 / Asia/Tokyo' : '食事の仮定を聞いた経験',
    experienced_when: kind === 'EPISODE' ? value.when : null,
    representative_id: kind === 'FACT' ? id : null,
    versions: [{ content_version: 1, created_at: '2026-09-13T00:00:00Z', content_erased: false,
      sources: [{ source_id: EPISODE, revision: 2, role: 'user', start: 0, end: 8, stated_at: '2026-09-13T00:00:00Z' }] }],
    references: [{ id: EPISODE, episode_id: EPISODE, episode_version: 1, fact_id: FACT, fact_version: 1, sources: [], valid: true }],
    merges: [],
  }
}
let records: EpisodicMemory[]
let requests: { method: string; body: { five_w?: FiveW; expected_version: number; idempotency_key: string } }[]
let failure: number
let block: Promise<void> | null
beforeEach(() => {
  records = [record(EPISODE, 'EPISODE'), record(FACT, 'FACT')]
  requests = []
  failure = 0
  block = null
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (!url.includes('episodic-memories')) return new Response('[]')
    if (!init?.method) return new Response(JSON.stringify(records))
    const body = JSON.parse(String(init.body))
    requests.push({ method: init.method, body })
    if (block) await block
    if (failure) return new Response(JSON.stringify({ reason_code: 'SEMANTIC_PRIVACY_DENIED' }), { status: failure })
    if (init.method === 'PATCH') {
      records = records.map(r => r.id === FACT ? { ...r, content_version: 2, five_w: body.five_w,
        normalized_text: '訂正した内容', references: [] } : { ...r, status: 'INACTIVE', five_w: null, normalized_text: '' })
      return new Response(JSON.stringify(records[1]))
    }
    records = records.map(r => ({ ...r, status: r.kind === 'FACT' ? 'DELETED' : 'INACTIVE',
      five_w: null, normalized_text: '', versions: r.versions.map(v => ({ ...v, content_erased: true })) }))
    return new Response(null, { status: 204 })
  }))
})
afterEach(() => vi.unstubAllGlobals())

async function edit() {
  await fireEvent.click(await screen.findByRole('button', { name: `Factを訂正 ${FACT}` }))
  return screen.getByRole('form', { name: '取得情報の訂正' })
}

test('経験とFact・仮定・元日時精度・出典と参照を分けて表示する', async () => {
  render(EpisodicMemoryManagement, { character: 'miori' })
  const fact = await screen.findByRole('article', { name: `FACT ${FACT}` })
  expect(screen.getByRole('article', { name: `EPISODE ${EPISODE}` })).toBeTruthy()
  expect(within(fact).getByText(/2026年9月/)).toBeTruthy()
  expect(within(fact).getByText('文脈：仮定')).toBeTruthy()
  expect(within(fact).getByText(/出典版 2/)).toBeTruthy()
  expect(within(fact).getAllByRole('link').length).toBeGreaterThan(0)
  expect(screen.queryByRole('button', { name: `Factを訂正 ${EPISODE}` })).toBeNull()
})

test('人物と月精度を編集し、空欄の日時・理由を補完せず版付きで送る', async () => {
  render(EpisodicMemoryManagement, { character: 'miori' })
  const form = await edit()
  await fireEvent.input(within(form).getByLabelText('人物名 1'), { target: { value: '友人' } })
  await fireEvent.input(within(form).getByLabelText('対象'), { target: { value: 'そば' } })
  await fireEvent.input(within(form).getByLabelText('発生日時 月'), { target: { value: '10' } })
  await fireEvent.submit(form)
  await waitFor(() => expect(requests).toHaveLength(1))
  const sent = requests[0].body
  expect(sent.expected_version).toBe(1)
  expect(sent.idempotency_key).toBeTruthy()
  expect(sent.five_w?.who[0]).toEqual({ name: '友人', role: 'ACTOR', entity_id: null })
  expect(sent.five_w?.when?.parts).toEqual({ ...emptyTimeParts(), year: 2026, month: 10 })
  expect(sent.five_w?.when?.timezone).toBe('Asia/Tokyo')
  expect(sent.five_w?.why).toBeNull()
  expect(sent.five_w?.context).toBe('HYPOTHETICAL')
  await waitFor(() => expect(screen.queryByRole('form')).toBeNull())
  expect(screen.queryByText(/ユーザーがうどん/)).toBeNull()
})

test('内容を変えない再試行は同じキーを使い二重送信を防ぐ', async () => {
  failure = 500
  let release = () => {}
  block = new Promise<void>(resolve => { release = resolve })
  render(EpisodicMemoryManagement, { character: 'miori' })
  const form = await edit()
  await fireEvent.submit(form)
  const save = screen.getByRole('button', { name: 'Factの訂正を保存' }) as HTMLButtonElement
  expect(save.disabled).toBe(true)
  release()
  block = null
  await waitFor(() => expect(save.disabled).toBe(false))
  await fireEvent.submit(form)
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[0].body.idempotency_key).toBe(requests[1].body.idempotency_key)
})

test('版競合を表示し再読み込みで古い編集内容を破棄する', async () => {
  failure = 409
  render(EpisodicMemoryManagement, { character: 'miori' })
  const form = await edit()
  await fireEvent.submit(form)
  expect((await screen.findByRole('alert')).textContent).toContain('記憶が変更されています')
  failure = 0
  await fireEvent.click(screen.getByRole('button', { name: '再読み込み' }))
  await waitFor(() => expect(screen.queryByRole('form')).toBeNull())
  expect(requests).toHaveLength(1)
})

test('privacy拒否後に内容を変えた再送は新しいキーで評価する', async () => {
  failure = 422
  render(EpisodicMemoryManagement, { character: 'miori' })
  const form = await edit()
  await fireEvent.submit(form)
  expect((await screen.findByRole('alert')).textContent).toContain('SEMANTIC_PRIVACY_DENIED')
  await fireEvent.input(within(form).getByLabelText('対象'), { target: { value: 'そば' } })
  failure = 0
  await fireEvent.submit(form)
  await waitFor(() => expect(requests).toHaveLength(2))
  expect(requests[1].body.idempotency_key).not.toBe(requests[0].body.idempotency_key)
})

test('削除確認のキャンセルでfocusを戻し、削除後は古い本文を表示しない', async () => {
  render(EpisodicMemoryManagement, { character: 'miori' })
  const button = await screen.findByRole('button', { name: `Factを削除 ${FACT}` })
  await fireEvent.click(button)
  const panel = screen.getByRole('region', { name: '取得情報の削除確認' })
  const cancel = within(panel).getByRole('button', { name: 'キャンセル' })
  expect(document.activeElement).toBe(cancel)
  await fireEvent.click(cancel)
  expect(document.activeElement).toBe(button)
  expect(requests).toHaveLength(0)
  await fireEvent.click(button)
  await fireEvent.click(screen.getByRole('button', { name: 'Factを削除する' }))
  await waitFor(() => expect(screen.queryByText(/ユーザーがうどん/)).toBeNull())
  expect(requests[0].method).toBe('DELETE')
  expect(requests[0].body.expected_version).toBe(1)
  expect(await screen.findByText('削除済み・第1版')).toBeTruthy()
  expect(screen.getByRole('article', { name: `EPISODE ${EPISODE}` })).toBeTruthy()
})

test('親画面のcharacter変更で古いFact編集を引き継がない', async () => {
  const view = render(MemoryManagement, { character: 'miori', onClose: () => {} })
  await edit()
  records = []
  await view.rerender({ character: 'other', onClose: () => {} })
  await waitFor(() => expect(screen.queryByRole('form')).toBeNull())
  expect(screen.queryByText(/ユーザーがうどん/)).toBeNull()
})


test('利用停止のFactを旧本文なしで再入力し訂正できる', async () => {
  records[1] = { ...records[1], status: 'INACTIVE', five_w: null, normalized_text: '' }
  render(EpisodicMemoryManagement, { character: 'miori' })
  const form = await edit()
  expect((within(form).getByLabelText('行為・出来事（必須）') as HTMLInputElement).value).toBe('')
  await fireEvent.input(within(form).getByLabelText('行為・出来事（必須）'), { target: { value: '散歩した' } })
  await fireEvent.submit(form)
  await waitFor(() => expect(requests).toHaveLength(1))
  expect(requests[0].body.five_w?.what.predicate).toBe('散歩した')
  expect(requests[0].body.five_w?.when).toBeNull()
})
