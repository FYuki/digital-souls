export type Availability = 'unknown' | 'available' | 'degraded' | 'unavailable'
export type EffectiveState = Availability | 'disabled'
export type ErrorCode = 'connection_failed' | 'authentication_failed' | 'protocol_error' | 'health_check_failed' | 'partial_failure'
export type AddonStatus = {
  connection_instance_id: string
  display_name: string
  source_type: 'external' | 'self_owned'
  desired_enabled: boolean
  availability: Availability
  effective_state: EffectiveState
  error_code: ErrorCode | null
  last_checked_at: string | null
}

const errorMessages: Record<ErrorCode, string> = {
  connection_failed: '接続できません',
  authentication_failed: '認証に失敗しました',
  protocol_error: '接続先との通信に問題があります',
  health_check_failed: '接続状態を確認できません',
  partial_failure: '一部機能に問題があります',
}

export const stateMessage = (item: AddonStatus): string => {
  switch (item.effective_state) {
    case 'disabled': return '無効'
    case 'unknown': return '状態を確認しています'
    case 'available': return '使用可能'
    case 'degraded': return errorMessages.partial_failure
    case 'unavailable': return `使用不可 — ${errorMessages[item.error_code ?? 'connection_failed']}`
  }
}

export function parseStatus(value: unknown): AddonStatus {
  if (typeof value !== 'object' || value === null) throw new Error('invalid_management_response')
  const v = value as Record<string, unknown>
  const states: unknown[] = ['unknown', 'available', 'degraded', 'unavailable']
  if (typeof v.connection_instance_id !== 'string'
    || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(v.connection_instance_id)
    || typeof v.display_name !== 'string' || !v.display_name.trim() || v.display_name.length > 128
    || (v.source_type !== 'external' && v.source_type !== 'self_owned')
    || typeof v.desired_enabled !== 'boolean' || !states.includes(v.availability)
    || v.effective_state !== (v.desired_enabled ? v.availability : 'disabled')
    || (v.source_type === 'external' && (v.availability === 'degraded' || v.error_code === 'partial_failure'))
    || !(v.error_code === null || (typeof v.error_code === 'string' && Object.hasOwn(errorMessages, v.error_code)))
    || !(v.last_checked_at === null || (typeof v.last_checked_at === 'string' && Number.isFinite(Date.parse(v.last_checked_at))))) {
    throw new Error('invalid_management_response')
  }
  // 表示するfieldだけを取り込み、接続設定やraw errorをdomain stateに残さない。
  return {
    connection_instance_id: v.connection_instance_id,
    display_name: v.display_name,
    source_type: v.source_type,
    desired_enabled: v.desired_enabled,
    availability: v.availability as Availability,
    effective_state: v.effective_state as EffectiveState,
    error_code: v.error_code as ErrorCode | null,
    last_checked_at: v.last_checked_at as string | null,
  }
}

const base = '/api/addon-admin/connections'
async function request(url: string, signal: AbortSignal, init?: RequestInit): Promise<unknown> {
  const response = await fetch(url, { ...init, signal, cache: 'no-store' })
  if (!response.ok) throw new Error('management_request_failed')
  return response.json()
}

export async function listAddons(signal: AbortSignal): Promise<AddonStatus[]> {
  const value = await request(base, signal)
  if (!Array.isArray(value)) throw new Error('invalid_management_response')
  const items = value.map(parseStatus)
  if (new Set(items.map((item) => item.connection_instance_id)).size !== items.length) {
    throw new Error('invalid_management_response')
  }
  return items
}

export async function setAddonEnabled(id: string, enabled: boolean, signal: AbortSignal): Promise<AddonStatus> {
  const result = parseStatus(await request(`${base}/${encodeURIComponent(id)}`, signal, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ desired_enabled: enabled }),
  }))
  if (result.connection_instance_id !== id) throw new Error('invalid_management_response')
  return result
}
