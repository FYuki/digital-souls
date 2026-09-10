import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import { afterEach, expect, test, vi } from 'vitest'
import ToolUseStatus from './ToolUseStatus.svelte'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

test.each(['always', 'once', 'reject'])('3択 %s の回答を保存してから同じ要求だけを続行する', async (choice) => {
  let resolveAnswer: (response: Response) => void = () => undefined
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => {
    if (url.endsWith('/answer')) return await new Promise<Response>(resolve => { resolveAnswer = resolve })
    if (options?.method === 'POST') return new Response('{}')
    if (url.includes('/status/')) return new Response(JSON.stringify({ state: 'waiting', sources: [], confirmation_id: 'confirmation-1' }))
    return new Response(JSON.stringify({ requests: [{
      id: 'confirmation-1', choice: null, operation_group: 'high_impact', scene: 'conversation',
      preview: { connection: 'テスト接続', operation: '<img src=x onerror=alert(1)>', target: '破棄可能な文書', arguments: { text: '変更後' } },
    }] }))
  })
  vi.stubGlobal('fetch', fetcher)
  const onContinue = vi.fn(async () => undefined)
  render(ToolUseStatus, { character: 'miori', conversationId: 'conversation-a', onContinue })
  const labels = { always: '常に承認する', once: '一度承認する', reject: '拒否する' }
  const button = await screen.findByRole('button', { name: labels[choice as keyof typeof labels] })
  expect(screen.queryByText('追加情報をお待ちしています。入力または音声で回答できます。')).toBeNull()
  expect(document.querySelector('img')).toBeNull()
  expect(screen.getByText('ハイリスク操作群・対話中')).toBeTruthy()
  await fireEvent.click(button)
  await fireEvent.click(button)
  expect(onContinue).not.toHaveBeenCalled()
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/answer'))).toHaveLength(1)
  expect(fetcher).toHaveBeenCalledWith('/api/addon-actions/requests/confirmation-1/answer', expect.objectContaining({
    body: JSON.stringify({ character: 'miori', session_id: 'conversation-a', choice }),
  }))
  resolveAnswer(new Response('{}'))
  await waitFor(() => expect(onContinue).toHaveBeenCalledOnce())
  expect(onContinue).toHaveBeenCalledWith('confirmation-1')
})

test('回答保存後の続行失敗では単回承認を再発行しない', async () => {
  const fetcher = vi.fn(async (url: string, options?: RequestInit) => new Response(JSON.stringify(
    options?.method === 'POST' ? {} : url.includes('/status/')
      ? { state: 'waiting', sources: [], confirmation_id: 'id' }
      : { requests: [{ id: 'id', choice: null, operation_group: 'normal', scene: 'conversation',
        preview: { connection: '接続', operation: '変更', target: '対象', arguments: {} } }] },
  )))
  vi.stubGlobal('fetch', fetcher)
  const onContinue = vi.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce(undefined)
  render(ToolUseStatus, { character: 'miori', conversationId: 'conversation-a', onContinue })
  await fireEvent.click(await screen.findByRole('button', { name: '一度承認する' }))
  await screen.findByRole('alert')
  await fireEvent.click(screen.getByRole('button', { name: '会話を続行' }))
  await waitFor(() => expect(onContinue).toHaveBeenCalledTimes(2))
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/answer'))).toHaveLength(1)
})
