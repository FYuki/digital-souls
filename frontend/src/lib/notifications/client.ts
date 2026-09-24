import { get, writable } from 'svelte/store'

export type NotificationState = 'unread' | 'read' | 'hidden'
export type Notification = {
  id: string; source_id: string; character_id: string; event_type: string
  created: number; expires: number; state: NotificationState; version: number
  kind: 'monitor' | 'result'; metadata: Record<string, string>
}
export type Source = {
  source_id: string; event_type: string; character_id: string
  enabled: boolean; configurable: boolean; available: boolean; history_incomplete: boolean; status: string
}
export type Detail = { state: string; text?: string; omitted?: boolean; kind?: 'monitor' | 'result' }
export type Listing = {
  notification_only?: boolean
  items: Notification[]; sources: Source[]; total: number; unread_count: number; next_offset: number | null
  retention: { days: number; max_per_user: number; history_incomplete: boolean; evicted_count: number }
}
export type Filters = { source_id: string; character_id: string; unread: boolean; hidden: boolean; offset: number }
const defaults: Filters = { source_id: '', character_id: '', unread: false, hidden: false, offset: 0 }
const empty: Listing = { items: [], sources: [], total: 0, unread_count: 0, next_offset: null,
  retention: { days: 30, max_per_user: 10000, history_incomplete: false, evicted_count: 0 } }

export const detailMessage = (state: string): string => ({
  expired: '提供元で保存期限が切れています。', deleted: '提供元で削除されています。',
  not_found: '提供元に対象の情報がありません。', permission_denied: '現在の権限では取得できません。',
  revision_mismatch: '対象の実行結果を確認できません。別の結果は表示しません。',
  budget_exceeded: '取得回数の上限に達しました。少し待ってから再試行してください。',
  confirmation_required: '取得には連携設定の確認が必要です。',
}[state] ?? '提供元から取得できません。時間をおいて再試行してください。')

export function createNotificationController(fetcher: typeof fetch = (...args) => fetch(...args)) {
  const store = writable({ ...empty, loading: false, error: '', pending: false, filters: { ...defaults } })
  let generation = 0
  let request: AbortController | null = null
  let timer: ReturnType<typeof setInterval> | null = null
  let stopped = false

  async function refresh() {
    const ticket = ++generation
    request?.abort()
    request = new AbortController()
    const signal = request.signal
    const filters = get(store).filters
    const query = new URLSearchParams({ unread: String(filters.unread), hidden: String(filters.hidden), offset: String(filters.offset) })
    if (filters.source_id) query.set('source_id', filters.source_id)
    if (filters.character_id) query.set('character_id', filters.character_id)
    store.update(s => ({ ...s, loading: true }))
    try {
      const response = await fetcher('/api/notifications?' + query, { signal, cache: 'no-store' })
      if (!response.ok) throw new Error('unavailable')
      const data = await response.json() as Listing
      if (!Array.isArray(data.items) || !Array.isArray(data.sources) || !Number.isInteger(data.unread_count)
        || !data.retention || typeof data.retention.history_incomplete !== 'boolean') throw new Error('invalid')
      if (ticket === generation && !stopped) store.update(s => ({ ...s, ...data, loading: false, error: '' }))
    } catch {
      if (ticket === generation && !stopped) store.update(s => ({ ...s, ...empty, loading: false, error: '通知を取得できません。再取得してください。' }))
    }
  }
  async function mutate(url: string, body: unknown) {
    if (get(store).pending) return
    store.update(s => ({ ...s, pending: true, error: '' }))
    let error = ''
    try {
      const response = await fetcher(url, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), cache: 'no-store' })
      if (!response.ok) error = response.status === 409
        ? '別の画面で状態が変更されました。最新の一覧を確認してください。'
        : '変更を保存できませんでした。現在の状態を確認してください。'
    } catch { error = '変更を保存できたか確認できません。最新の一覧を確認してください。' }
    await refresh()
    if (!stopped) store.update(s => ({ ...s, pending: false, error: error || s.error }))
  }
  return {
    subscribe: store.subscribe, refresh,
    start() {
      if (timer !== null) return
      stopped = false
      void refresh()
      timer = setInterval(() => { void refresh() }, 15000)
      window.addEventListener('focus', refresh)
      window.addEventListener('online', refresh)
    },
    destroy() {
      stopped = true; generation++; request?.abort()
      if (timer !== null) clearInterval(timer)
      timer = null
      window.removeEventListener('focus', refresh)
      window.removeEventListener('online', refresh)
    },
    filter(filters: Partial<Filters>) {
      store.update(s => ({ ...s, items: [], filters: { ...s.filters, offset: 0, ...filters } }))
      void refresh()
    },
    setState(item: Notification, state: NotificationState) {
      return mutate('/api/notifications/' + encodeURIComponent(item.id), { state, version: item.version })
    },
    preference(source: Source, enabled: boolean) {
      return mutate('/api/notifications/preferences/' + encodeURIComponent(source.source_id) + '/' + encodeURIComponent(source.event_type), { enabled })
    },
    async detail(item: Notification, signal: AbortSignal): Promise<Detail> {
      const response = await fetcher('/api/notifications/' + encodeURIComponent(item.id) + '/detail', { signal, cache: 'no-store' })
      if (!response.ok) return { state: response.status === 403 ? 'permission_denied' : 'unavailable' }
      return await response.json() as Detail
    },
  }
}
export type NotificationController = ReturnType<typeof createNotificationController>
