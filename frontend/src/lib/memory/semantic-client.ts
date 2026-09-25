import { requestHttp } from '../http-client'
import type { MemoryTime } from './episodic-client'

export type SemanticSource = {
  kind: 'CONVERSATION' | 'EPISODE' | 'MANUAL'; source_id: string; revision: number
  conversation_id: string | null
  span: { role: string; start: number; end: number; stated_at: string } | null
}
export type SemanticMemory = {
  id: string; character_id: string; formation_type: 'DIRECT_EXTRACTION' | 'EXPERIENCE_DERIVED'
  status: 'ACTIVE' | 'HISTORICAL' | 'SUPERSEDED' | 'CONFLICTED' | 'INACTIVE' | 'DELETED'
  content_version: number; confidence: number; created_at: string; updated_at: string; can_correct: boolean
  proposition: { subject: string; predicate: string; value: string; content: string; self_report: boolean
    mutability: string; valid_from: MemoryTime | null; valid_until: MemoryTime | null } | null
  sources: SemanticSource[]; reassessment_pending: boolean
  stamp: { policy_version: string; model_id: string; prompt_version: string
    extraction: { provider_id: string; model_id: string; model_digest: string; prompt_version: string } | null }
  versions: { content_version: number; sources: SemanticSource[]; created_at: string; content_erased: boolean }[]
  relations: { source_id: string; source_version: number; target_id: string; target_version: number
    relation: string; created_at: string }[]
}
const collection = (character: string) => `/api/characters/${encodeURIComponent(character)}/semantic-memories`
const semanticRequestError = (response: Response): Error => new Error(
  response.status === 409
    ? '記憶が変更されています。再読み込みしてください。'
    : response.status === 422 ? '訂正可能な内容と保存条件を確認してください。'
    : '記憶の操作に失敗しました。再試行できます。')

const request = (url: string, init?: RequestInit): Promise<unknown> => (
  requestHttp(url, init, semanticRequestError, 'json-or-null')
)
export const listSemanticMemories = (character: string) =>
  request(collection(character)) as Promise<SemanticMemory[]>
export const correctSemanticMemory = (character: string, record: SemanticMemory, value: string, key: string) =>
  request(`${collection(character)}/${record.id}`, { method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_version: record.content_version, idempotency_key: key, value }),
  }) as Promise<SemanticMemory>
export const deleteSemanticMemory = (character: string, record: SemanticMemory) =>
  request(`${collection(character)}/${record.id}`, { method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_version: record.content_version }),
  })
