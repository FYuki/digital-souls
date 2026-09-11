export type ApprovalChoice = 'always' | 'once' | 'reject'
export type Permission = 'unapproved' | 'always' | 'denied'
export type Scope = { connection_id: string; operation_group: 'normal' | 'high_impact'; scene: 'conversation' | 'autonomous' }
export type ApprovalRequest = Scope & {
  id: string; character_id: string; session_id: string; created_at: number; wait_until: number
  waiting: boolean; choice: ApprovalChoice | null; once_reserved: boolean; connection_available: boolean
  preview: { connection: string; operation: string; target: string; arguments?: unknown }
}
export type ApprovalSetting = Scope & {
  connection_token: string; connection_label: string; permission: Permission; remaining: number; reserved: number
}
export const groupLabel = (scope: Scope) => scope.operation_group === 'normal' ? '通常操作群' : 'ハイリスク操作群'
export const sceneLabel = (scope: Scope) => scope.scene === 'conversation' ? '対話中' : '会話外'
export const permissionLabel = (value: Permission) => ({ always: '常に承認', unapproved: '未承認（利用時に確認）', denied: '拒否（設定変更まで利用不可）' })[value]
export const choiceLabel = (value: ApprovalChoice) => ({ always: '常に承認する', once: '一度承認する', reject: '拒否する' })[value]

export async function approvalApi(path: string, init: RequestInit = {}): Promise<Record<string, unknown>> {
  const response = await fetch(`/api/addon-actions/admin/${path}`, { cache: 'no-store', ...init })
  if (!response.ok) throw new Error('承認情報を取得・保存できませんでした。状態を再取得してください。')
  const body: unknown = await response.json()
  if (body === null || typeof body !== 'object' || Array.isArray(body)) throw new Error('invalid approval response')
  return body as Record<string, unknown>
}

function scope(value: Record<string, unknown>): boolean {
  return typeof value.connection_id === 'string' && ['normal', 'high_impact'].includes(String(value.operation_group))
    && ['conversation', 'autonomous'].includes(String(value.scene))
}

export function parseRequests(body: Record<string, unknown>): { requests: ApprovalRequest[]; next: string | null } {
  if (!Array.isArray(body.requests) || !body.requests.every(item => item && scope(item)
    && ['id', 'character_id', 'session_id'].every(key => typeof item[key] === 'string')
    && ['waiting', 'connection_available', 'once_reserved'].every(key => typeof item[key] === 'boolean')
    && Number.isFinite(item.created_at) && Number.isFinite(item.wait_until)
    && [null, 'always', 'once', 'reject'].includes(item.choice)
    && item.preview && ['connection', 'operation', 'target'].every(key => typeof item.preview[key] === 'string'))
    || !(body.next_cursor === null || typeof body.next_cursor === 'string')) throw new Error('invalid approval queue')
  return { requests: body.requests as ApprovalRequest[], next: body.next_cursor as string | null }
}

export function parseSettings(body: Record<string, unknown>): ApprovalSetting[] {
  if (!Array.isArray(body.permissions) || !body.permissions.every(item => item && scope(item)
    && typeof item.connection_token === 'string' && typeof item.connection_label === 'string'
    && ['always', 'unapproved', 'denied'].includes(item.permission)
    && Number.isSafeInteger(item.remaining) && item.remaining >= 0 && Number.isSafeInteger(item.reserved) && item.reserved >= 0)) {
    throw new Error('invalid approval settings')
  }
  return body.permissions as ApprovalSetting[]
}
