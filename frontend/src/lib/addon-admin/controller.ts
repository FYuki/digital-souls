import { writable } from 'svelte/store'
import { listAddons, setAddonEnabled, SettingsDurabilityError, ManagementError, managementError, type AddonStatus } from './client'

export type ManagementState = {
  items: AddonStatus[]
  loading: boolean
  error: string | null
  pending: Set<string>
  rowErrors: Record<string, string>
  retryEnabled: Record<string, boolean>
}

type Gateway = { list: typeof listAddons; setEnabled: typeof setAddonEnabled }

const group = (item: AddonStatus): number => !item.desired_enabled ? 2
  : ['unavailable', 'degraded'].includes(item.availability) ? 0 : 1
export const sortedAddons = (items: AddonStatus[]): AddonStatus[] => [...items].sort((a, b) => (
  group(a) - group(b) || a.display_name.localeCompare(b.display_name, 'ja')
  || a.connection_instance_id.localeCompare(b.connection_instance_id)
))
export function aggregateBadge(items: AddonStatus[]): 'error' | 'warning' | null {
  const enabled = items.filter((item) => item.desired_enabled)
  if (enabled.some((item) => item.availability === 'unavailable')) return 'error'
  if (enabled.some((item) => item.source_type === 'self_owned' && item.availability === 'degraded')) return 'warning'
  return null
}

export function createAddonController(gateway: Gateway = { list: listAddons, setEnabled: setAddonEnabled }) {
  let state: ManagementState = { items: [], loading: true, error: null, pending: new Set(), rowErrors: {}, retryEnabled: {} }
  const store = writable(state)
  const publish = (next: Partial<ManagementState>) => { state = { ...state, ...next }; store.set(state) }
  let epoch = 0
  let closed = false
  let reading = false
  let timer: ReturnType<typeof setInterval> | undefined
  const requests = new Set<AbortController>()
  async function bounded<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const abort = new AbortController()
    requests.add(abort)
    const deadline = setTimeout(() => abort.abort(), 30000)
    try { return await operation(abort.signal) }
    finally { clearTimeout(deadline); requests.delete(abort) }
  }
  async function refresh() {
    if (closed || reading || state.pending.size > 0) return
    reading = true
    const revision = epoch
    try {
      const items = await bounded(gateway.list)
      if (!closed && epoch === revision) publish({ items, error: null })
    } catch {
      if (!closed && epoch === revision) publish({ error: '連携の状態を取得できません。再試行してください。' })
    } finally {
      reading = false
      if (!closed) publish({ loading: false })
    }
  }
  async function toggle(id: string, enabled: boolean) {
    if (closed || state.pending.has(id)) return
    epoch += 1
    let recheck = false
    const retryEnabled = { ...state.retryEnabled }
    delete retryEnabled[id]
    publish({ pending: new Set([...state.pending, id]), rowErrors: { ...state.rowErrors, [id]: '' }, retryEnabled })
    try {
      const item = await bounded((signal) => gateway.setEnabled(id, enabled, signal))
      if (!closed) publish({ items: state.items.map((old) => old.connection_instance_id === id ? item : old) })
    } catch (error) {
      recheck = error instanceof SettingsDurabilityError
      const message = recheck
        ? '変更は反映されましたが、保存の完了を確認できません。同じ設定を再度保存してください。'
        : error instanceof ManagementError ? managementError(error) : '変更を保存できませんでした。状態を再確認してください。'
      if (!closed) publish({ rowErrors: { ...state.rowErrors, [id]: message }, retryEnabled: { ...state.retryEnabled, [id]: enabled } })
    } finally {
      if (!closed) {
        const pending = new Set(state.pending)
        pending.delete(id)
        publish({ pending })
      }
    }
    if (recheck) await refresh()
  }
  return {
    subscribe: store.subscribe, refresh, toggle,
    async retry(id: string) {
      const enabled = state.retryEnabled[id]
      if (typeof enabled === 'boolean') await toggle(id, enabled)
    },
    start() {
      if (timer || closed) return
      void refresh()
      timer = setInterval(() => { void refresh() }, 5000)
    },
    destroy() {
      closed = true
      epoch += 1
      clearInterval(timer)
      for (const request of requests) request.abort()
    },
  }
}
export type AddonController = ReturnType<typeof createAddonController>
