import { render, screen, waitFor, within } from '@testing-library/svelte'
import { afterEach, expect, test, vi } from 'vitest'
import MemoryManagement from './lib/MemoryManagement.svelte'
import type { SemanticMemory } from './lib/memory/semantic-client'

function memory(character: string): SemanticMemory {
  return {
    id: character, character_id: character, formation_type: 'DIRECT_EXTRACTION',
    status: 'ACTIVE', content_version: 1, confidence: 1, can_correct: true,
    created_at: '2026-09-15T00:00:00Z', updated_at: '2026-09-15T00:00:00Z',
    reassessment_pending: false,
    proposition: { subject: 'ユーザー', predicate: '花の好み', value: character,
      content: `${character}だけの意味記憶`, self_report: true, mutability: 'CHANGEABLE',
      valid_from: null, valid_until: null },
    sources: [], versions: [], relations: [],
    stamp: { policy_version: 'fixture', model_id: 'fixture', prompt_version: 'fixture', extraction: null },
  }
}

afterEach(() => vi.unstubAllGlobals())

test('character切替後に以前の意味記憶の取得が完了しても現在の画面へ混入しない', async () => {
  let release!: (response: Response) => void
  const delayed = new Promise<Response>(resolve => { release = resolve })
  const requests: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (!url.endsWith('/semantic-memories')) return new Response('[]')
    requests.push(url)
    if (url.includes('/miori/')) return delayed
    return new Response(JSON.stringify([memory('other')]))
  }))
  const view = render(MemoryManagement, { character: 'miori', onClose: () => undefined })
  await waitFor(() => expect(requests.some(url => url.includes('/miori/'))).toBe(true))
  await view.rerender({ character: 'other', onClose: () => undefined })
  const region = screen.getByRole('region', { name: '意味記憶' })
  expect(await within(region).findByText('otherだけの意味記憶')).toBeTruthy()
  release(new Response(JSON.stringify([memory('miori')])))
  await delayed
  await new Promise(resolve => setTimeout(resolve, 0))
  expect(within(region).queryByText('mioriだけの意味記憶')).toBeNull()
  expect(within(region).getByText('otherだけの意味記憶')).toBeTruthy()
})