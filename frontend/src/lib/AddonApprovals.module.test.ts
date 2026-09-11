import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte'
import AddonApprovals from './AddonApprovals.svelte'
import type { ApprovalRequest, ApprovalSetting } from './addon-admin/approvals'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const pending: ApprovalRequest = { id: 'request', connection_id: 'one', operation_group: 'high_impact', scene: 'conversation',
  character_id: 'miori', session_id: 'original', waiting: true, wait_until: Date.now() / 1000 + 600,
  created_at: Date.now() / 1000, choice: null, connection_available: true, once_reserved: false,
  preview: { connection: '資料', operation: '更新', target: '検証用文書', arguments: { text: '<script>攻撃</script>' } },
}
const setting: ApprovalSetting = { connection_id: 'one', connection_token: 'token', connection_label: '資料',
  operation_group: 'high_impact', scene: 'conversation', permission: 'unapproved', remaining: 0, reserved: 0 }
function setup(item = pending, continueResult: () => Promise<Response> = async () => Response.json({ turn: 'result' })) {
  let current = { ...item }
  const fetcher = vi.fn(async (input: string, init?: RequestInit) => {
    if (input.endsWith('/continue')) return continueResult()
    if (input.endsWith('/answer')) {
      current = { ...current, choice: JSON.parse(String(init?.body)).choice }
      return Response.json({ request: current })
    }
    if (init?.method === 'PUT') return Response.json({ setting: { permission: 'unapproved', remaining: 0, reserved: 0 } })
    if (input.includes('/permissions')) return Response.json({ permissions: [setting, { ...setting, scene: 'autonomous', permission: 'denied' }] })
    return Response.json({ requests: [current], next_cursor: null })
  })
  vi.stubGlobal('fetch', fetcher)
  const onContinued = vi.fn()
  render(AddonApprovals, { onContinued })
  return { fetcher, onContinued }
}

test('待機中の明示回答だけが元要求の続行を呼び、二重クリックを抑える', async () => {
  let finish!: (response: Response) => void
  const { fetcher, onContinued } = setup(pending, () => new Promise(resolve => { finish = resolve }))
  const button = await screen.findByRole('button', { name: '一度承認する' })
  await fireEvent.click(button); await fireEvent.click(button)
  await waitFor(() => expect(finish).toBeTypeOf('function'))
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/answer'))).toHaveLength(1)
  expect(fetcher.mock.calls.find(([url]) => url.endsWith('/answer'))?.[1]?.body).toBe(JSON.stringify({ choice: 'once' }))
  finish(Response.json({ turn: 'result' }))
  await waitFor(() => expect(onContinued).toHaveBeenCalledWith(expect.objectContaining({ session_id: 'original' }), { turn: 'result' }))
})

test('待機終了後の単回承認は将来向けと表示し、元操作を続行しない', async () => {
  const { fetcher } = setup({ ...pending, waiting: false })
  await screen.findByText(/将来の呼び出し1回分/)
  await fireEvent.click(screen.getByRole('button', { name: '一度承認する' }))
  await screen.findByText('承認を保存しました。元の操作は再開せず、今後の利用に適用します。')
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('/continue'))).toBe(false)
})

test('未承認への変更は表示した承認範囲へ限定し、対話中の永続拒否は作らない', async () => {
  const { fetcher } = setup()
  const row = await screen.findByRole('listitem', { name: '資料・ハイリスク操作群・会話外' })
  expect(within(row).getByText('設定：拒否（設定変更まで利用不可）')).toBeTruthy()
  const conversation = screen.getByRole('listitem', { name: '資料・ハイリスク操作群・対話中' })
  expect(within(conversation).queryByRole('button', { name: '拒否へ変更' })).toBeNull()
  await fireEvent.click(within(row).getByRole('button', { name: '未承認へ戻す' }))
  await screen.findByText('未承認へ戻しました。未使用の単回許可と要求への予約を取り消しました。')
  const call = fetcher.mock.calls.find(([, init]) => init?.method === 'PUT')
  expect(JSON.parse(String(call?.[1]?.body))).toEqual({ connection_id: 'one', connection_token: 'token',
    operation_group: 'high_impact', scene: 'autonomous', permission: 'unapproved' })
})

test('続行失敗は承認の再発行なしに続行だけ再試行できる', async () => {
  const continuation = vi.fn().mockResolvedValueOnce(new Response('private-stack-secret', { status: 409 }))
    .mockResolvedValue(Response.json({ state: 'ended' }))
  const { fetcher } = setup(pending, continuation)
  await fireEvent.click(await screen.findByRole('button', { name: '一度承認する' }))
  const retry = await screen.findByRole('button', { name: '保存した回答で続行を再試行' })
  await waitFor(() => expect(retry.hasAttribute('disabled')).toBe(false))
  await fireEvent.click(retry)
  await screen.findByText('元の操作の待機は終了しています。元の操作は再開しません。')
  expect(fetcher.mock.calls.filter(([url]) => url.endsWith('/answer'))).toHaveLength(1)
  expect(continuation).toHaveBeenCalledTimes(2)
  expect(document.body.textContent).not.toContain('private-stack-secret')
})

test('取得だけでは回答済み要求を再開せず、接続変更済み要求の回答を無効化する', async () => {
  const { fetcher } = setup({ ...pending, connection_available: false })
  expect((await screen.findByRole('button', { name: '一度承認する' })).hasAttribute('disabled')).toBe(true)
  expect(document.querySelector('script')).toBeNull()
  expect(fetcher.mock.calls.every(([, init]) => !init?.method)).toBe(true)
})
