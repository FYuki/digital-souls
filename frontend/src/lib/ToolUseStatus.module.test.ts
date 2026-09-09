import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import { afterEach, expect, test, vi } from 'vitest'
import ToolUseStatus from './ToolUseStatus.svelte'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

test('入力待ちの表示と明示停止を現在の会話へ送る', async () => {
  const fetcher = vi.fn(async (_url: string, options?: RequestInit) => new Response(JSON.stringify(
    options?.method === 'POST' ? { state: 'stopped' } : { state: 'waiting', sources: [] },
  )))
  vi.stubGlobal('fetch', fetcher)
  const onStop = vi.fn(async () => undefined)
  render(ToolUseStatus, { character: 'miori', conversationId: 'conversation-a', onStop })
  await screen.findByText('追加情報をお待ちしています。入力または音声で回答できます。')
  await fireEvent.click(screen.getByRole('button', { name: '外部操作を停止' }))
  await waitFor(() => expect(onStop).toHaveBeenCalledOnce())
  expect(fetcher).toHaveBeenCalledWith('/api/tool-use/stop', expect.objectContaining({
    method: 'POST', body: JSON.stringify({ character: 'miori', conversation_id: 'conversation-a' }),
  }))
})

test('出典は文字として表示し、ページ終了で入力待ちを破棄する', async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({
    state: 'waiting', sources: [{ label: '<img src=x onerror=alert(1)>', source_id: 'id' }],
  })))
  vi.stubGlobal('fetch', fetcher)
  render(ToolUseStatus, { character: 'miori', conversationId: 'conversation-b' })
  await screen.findByText('今回参照した情報')
  expect(document.querySelector('img')).toBeNull()
  window.dispatchEvent(new Event('pagehide'))
  expect(fetcher).toHaveBeenCalledWith('/api/tool-use/stop', expect.objectContaining({ keepalive: true }))
})

test('初回status待ちの離脱でも停止し、状態取得をキャッシュしない', () => {
  const fetcher = vi.fn((_url: string, options?: RequestInit) => options?.method === 'POST'
    ? Promise.resolve(new Response('{}')) : new Promise<Response>(() => undefined))
  vi.stubGlobal('fetch', fetcher)
  const component = render(ToolUseStatus, { character: 'miori', conversationId: 'pending' })
  expect(fetcher).toHaveBeenCalledWith(expect.stringContaining('/status/'), expect.objectContaining({ cache: 'no-store' }))
  window.dispatchEvent(new Event('pagehide'))
  expect(fetcher).toHaveBeenCalledWith('/api/tool-use/stop', expect.objectContaining({ method: 'POST' }))
  fetcher.mockClear()
  component.unmount()
  expect(fetcher).toHaveBeenCalledWith('/api/tool-use/stop', expect.objectContaining({ method: 'POST' }))
})
