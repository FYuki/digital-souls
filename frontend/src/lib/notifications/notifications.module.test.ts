import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { get } from 'svelte/store'
import NotificationCenter from '../NotificationCenter.svelte'
import { createNotificationController, type Listing, type Notification } from './client'

const item: Notification = { id: 'n1', source_id: 'provider', character_id: 'miori', event_type: 'task.completed',
  created: 1800000000, expires: 1802592000, state: 'unread', version: 1, kind: 'monitor', metadata: { task_ref: 'task-1' } }
const listing = (): Listing => ({ items: [{ ...item }], total: 1, unread_count: 1, next_offset: null,
  sources: [{ source_id: 'provider', character_id: 'miori', event_type: 'task.completed', enabled: true,
    configurable: true, available: true, history_incomplete: false, status: 'ready' }],
  retention: { days: 30, max_per_user: 10000, history_incomplete: false, evicted_count: 0 } })
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('通知画面', () => {
  it('一覧・詳細取得では未読のままで、確認操作だけを通知IDと版で送る', async () => {
    const data = listing()
    const fetcher = vi.fn<typeof fetch>(async (input, init) => {
      if (init?.method === 'PATCH') {
        expect(JSON.parse(String(init.body))).toEqual({ state: 'read', version: 1 })
        data.items[0] = { ...item, state: 'read', version: 2 }; data.unread_count = 0
        return response(data.items[0])
      }
      if (String(input).endsWith('/detail')) return response({ state: 'available', text: '<script>悪意ある本文</script>', kind: 'monitor' })
      return response(data)
    })
    const controller = createNotificationController(fetcher)
    render(NotificationCenter, { controller, onClose: vi.fn() })
    await screen.findByText('1件未読')
    await fireEvent.click(screen.getByRole('button', { name: '処理完了 task-1の詳細' }))
    await screen.findByText('<script>悪意ある本文</script>')
    expect(document.querySelector('script')).toBeNull()
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'PATCH')).toHaveLength(0)
    await fireEvent.click(screen.getByRole('button', { name: '確認して既読にする' }))
    await screen.findByText('0件未読')
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'PATCH')).toHaveLength(1)
    controller.destroy()
  })

  it('取得失敗と、閉じた後に到着した詳細を既読へ変換しない', async () => {
    let resolve: (value: Response) => void = () => undefined
    const fetcher = vi.fn<typeof fetch>(async input => String(input).endsWith('/detail')
      ? await new Promise<Response>(done => { resolve = done }) : response(listing()))
    const controller = createNotificationController(fetcher)
    render(NotificationCenter, { controller, onClose: vi.fn() })
    await screen.findByText('1件未読')
    await fireEvent.click(screen.getByRole('button', { name: '処理完了 task-1の詳細' }))
    await fireEvent.click(screen.getByRole('button', { name: '詳細を閉じる' }))
    resolve(response({ state: 'available', text: '遅延した詳細' }))
    await new Promise(done => setTimeout(done, 0))
    expect(screen.queryByText('遅延した詳細')).toBeNull()
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'PATCH')).toHaveLength(0)
    controller.destroy()
  })

  it('種類別設定、絞り込み、履歴不足を会話なしで扱う', async () => {
    const data = listing()
    data.retention.history_incomplete = true
    const fetcher = vi.fn<typeof fetch>(async (_input, init) => {
      if (init?.method === 'PATCH') { data.sources[0].enabled = false; return new Response(null, { status: 204 }) }
      return response(data)
    })
    const controller = createNotificationController(fetcher)
    render(NotificationCenter, { controller, onClose: vi.fn() })
    await screen.findByText(/件数上限により削除された通知があります/)
    await fireEvent.change(screen.getByLabelText('担当'), { target: { value: 'miori' } })
    await waitFor(() => expect(fetcher.mock.calls.some(([input]) => String(input).includes('character_id=miori'))).toBe(true))
    await fireEvent.click(screen.getByRole('button', { name: '通知設定' }))
    await fireEvent.click(screen.getByRole('switch', { name: 'provider 処理完了の通知' }))
    await waitFor(() => expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false'))
    expect(fetcher.mock.calls.some(([input, init]) => String(input) === '/api/notifications/preferences/provider/task.completed'
      && JSON.parse(String(init?.body)).enabled === false)).toBe(true)
    controller.destroy()
  })

  it('端末間競合では最新を再取得し、新着の未読を勝手に消さない', async () => {
    let count = 0
    const fetcher = vi.fn<typeof fetch>(async (_input, init) => {
      if (init?.method === 'PATCH') return response({}, 409)
      const data = listing()
      if (count++ > 0) { data.items.push({ ...item, id: 'n2' }); data.total = 2; data.unread_count = 2 }
      return response(data)
    })
    const controller = createNotificationController(fetcher)
    await controller.refresh()
    await controller.setState(item, 'read')
    expect(get(controller).error).toContain('別の画面')
    expect(get(controller).unread_count).toBe(2)
    expect(get(controller).items.every(item => item.state === 'unread')).toBe(true)
    controller.destroy()
  })
})
