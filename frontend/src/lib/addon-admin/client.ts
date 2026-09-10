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
  settings_revision?: number
  last_success_at?: string | null
  last_attempt_at?: string | null
  last_check_error?: string | null
}

const errorMessages: Record<ErrorCode, string> = {
  connection_failed: '接続できません',
  authentication_failed: '認証に失敗しました',
  protocol_error: '接続先との通信に問題があります',
  health_check_failed: '接続状態を確認できません',
  partial_failure: '一部機能に問題があります',
}

const checkMessages: Record<string, string> = { ...errorMessages, confirmation_timeout: '接続確認がタイムアウトしました' }
export const checkErrorMessage = (item: AddonStatus): string => item.last_check_error ? checkMessages[item.last_check_error] ?? '' : ''

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
    ...(typeof v.settings_revision === 'number' ? {
      settings_revision: v.settings_revision,
      last_success_at: typeof v.last_success_at === 'string' ? v.last_success_at : null,
      last_attempt_at: typeof v.last_attempt_at === 'string' ? v.last_attempt_at : null,
      last_check_error: typeof v.last_check_error === 'string' && Object.hasOwn(checkMessages, v.last_check_error) ? v.last_check_error : null,
    } : {}),
  }
}

export class SettingsDurabilityError extends Error {}
export class ManagementError extends Error {}
export function managementError(error: unknown): string {
  switch (error instanceof ManagementError ? error.message : '') {
    case 'connection_busy': return '実行完了後に再試行してください。'
    case 'connection_changed': return '設定が変更されました。最新の状態を確認して再試行してください。'
    case 'connection_unconfirmed': return '接続を確認できなかったため、ONにできませんでした。'
    case 'invalid_connection_settings':
    case 'invalid_management_request': return '入力内容を確認してください。'
    case 'connection_not_found': return 'この接続は削除されています。'
    default: return '操作を完了できませんでした。状態を確認して再試行してください。'
  }
}
export function confirmationMessage(item: AddonStatus): string {
  if (item.settings_revision === undefined) return ''
  if (!item.last_success_at) return item.last_check_error ? '接続未確認（前回確認失敗）' : '接続未確認'
  if (item.availability === 'unavailable') return '接続不可'
  return '接続確認済み'
}

const base = '/api/addon-admin/connections'
async function request(url: string, signal: AbortSignal, init?: RequestInit): Promise<unknown> {
  const response = await fetch(url, { ...init, signal, cache: 'no-store' })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    if (response.status === 503 && body?.detail === 'settings_durability_uncertain') {
      throw new SettingsDurabilityError('settings_durability_uncertain')
    }
    throw new ManagementError(typeof body?.detail === 'string' ? body.detail : 'management_request_failed')
  }
  return response.status === 204 ? null : response.json()
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


export type ConnectionSettings = { transport: 'streamable_http'; endpoint: string; auth: 'none' | 'bearer' }
  | { transport: 'stdio'; command: string; args: string[] }
export type ConnectionInput = { display_name: string; settings: ConnectionSettings }
export type ConnectionDetail = AddonStatus & ConnectionInput & {
  credential_set: boolean
  capabilities: { counts?: { tools: number; resources: number; prompts: number }; tools?: { name: string; description: string; status: string }[] }
}
const external = '/api/addon-admin/external-connections'
function parseDetail(value: unknown): ConnectionDetail {
  const status = parseStatus(value)
  const v = value as Record<string, unknown>
  if (typeof v.settings !== 'object' || v.settings === null || typeof v.credential_set !== 'boolean'
    || typeof v.capabilities !== 'object' || v.capabilities === null) throw new Error('invalid_management_response')
  const settings = v.settings as Record<string, unknown>
  let parsed: ConnectionSettings
  if (settings.transport === 'streamable_http' && typeof settings.endpoint === 'string' && (settings.auth === 'none' || settings.auth === 'bearer')) {
    parsed = { transport: 'streamable_http', endpoint: settings.endpoint, auth: settings.auth }
  } else if (settings.transport === 'stdio' && typeof settings.command === 'string' && Array.isArray(settings.args) && settings.args.every((arg) => typeof arg === 'string')) {
    parsed = { transport: 'stdio', command: settings.command, args: settings.args }
  } else throw new Error('invalid_management_response')
  const capabilities = v.capabilities as ConnectionDetail['capabilities']
  if (capabilities.tools !== undefined && (!Array.isArray(capabilities.tools) || capabilities.tools.some((tool) =>
    typeof tool !== 'object' || tool === null || typeof tool.name !== 'string' || typeof tool.description !== 'string' || typeof tool.status !== 'string'))) {
    throw new Error('invalid_management_response')
  }
  return { ...status, settings: parsed, credential_set: v.credential_set,
    capabilities: { counts: capabilities.counts, tools: capabilities.tools?.map(({ name, description, status }) => ({ name, description, status })) } }
}
export async function getConnection(id: string, signal: AbortSignal): Promise<ConnectionDetail> {
  return parseDetail(await request(`${external}/${encodeURIComponent(id)}`, signal))
}
export async function saveConnection(id: string | null, input: ConnectionInput, signal: AbortSignal): Promise<ConnectionDetail> {
  return parseDetail(await request(id ? `${external}/${encodeURIComponent(id)}` : external, signal, {
    method: id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  }))
}
export async function saveCredential(id: string, token: string, signal: AbortSignal): Promise<ConnectionDetail> {
  return parseDetail(await request(`${external}/${encodeURIComponent(id)}/credential`, signal, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
  }))
}
export async function checkConnection(id: string, signal: AbortSignal): Promise<ConnectionDetail> {
  return parseDetail(await request(`${external}/${encodeURIComponent(id)}/check`, signal, { method: 'POST' }))
}
export async function deleteConnection(id: string, signal: AbortSignal): Promise<void> {
  await request(`${external}/${encodeURIComponent(id)}`, signal, { method: 'DELETE' })
}
