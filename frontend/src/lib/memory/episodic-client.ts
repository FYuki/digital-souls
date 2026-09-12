export type TimeParts = {
  year: number | null; month: number | null; day: number | null
  hour: number | null; minute: number | null; second: number | null
}
export type MemoryTime = {
  parts: TimeParts; end: TimeParts | null; range_kind: 'POINT' | 'UNCERTAINTY' | 'DURATION'
  timezone: string; reference_at: string
}
export type Person = {
  name: string; role: 'ACTOR' | 'PARTICIPANT' | 'SPEAKER' | 'LISTENER' | 'TOPIC'
  entity_id: string | null
}
export type FiveW = {
  who: Person[]
  what: { predicate: string; object: string | null; polarity: 'AFFIRMED' | 'NEGATED' | 'UNKNOWN'
    actuality: 'OCCURRED' | 'PLANNED' | 'CONDITIONAL' | 'UNKNOWN' }
  when: MemoryTime | null
  where: { name: string; entity_id: string | null } | null
  why: string | null
  context: 'REPORTED' | 'HYPOTHETICAL' | 'FICTIONAL' | 'UNKNOWN'
}
type Source = {
  source_id: string; revision: number; role: string; start: number; end: number; stated_at: string
}
export type EpisodicMemory = {
  id: string; kind: 'EPISODE' | 'FACT'; character_id: string; conversation_id: string | null
  content_version: number; status: 'ACTIVE' | 'INACTIVE' | 'DELETED'; stored_status: string
  five_w: FiveW | null; normalized_text: string; experienced_when: MemoryTime | null
  representative_id: string | null
  versions: { content_version: number; sources: Source[]; created_at: string; content_erased: boolean }[]
  references: { id: string; episode_id: string; episode_version: number; fact_id: string
    fact_version: number; sources: Source[]; valid: boolean }[]
  merges: { id: string; source_fact_id: string; source_version: number; target_fact_id: string
    target_version: number; conversation_id: string; policy: string; evidence: Source[]; valid: boolean }[]
}
export class EpisodicRequestError extends Error {
  constructor(readonly status: number, readonly reason: string | null = null) {
    super(status === 409 ? '記憶が変更されています。再読み込みしてから編集してください。'
      : status === 422 ? '保存条件または入力内容を確認してください。'
      : '記憶の操作に失敗しました。')
  }
}
const collection = (character: string) => `/api/characters/${encodeURIComponent(character)}/episodic-memories`
async function request(url: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(url, init)
  if (!response.ok) {
    let reason: string | null = null
    if (response.status === 422) {
      const body: unknown = await response.json().catch(() => null)
      if (body && typeof body === 'object' && 'reason_code' in body && typeof body.reason_code === 'string') {
        reason = body.reason_code
      }
    }
    throw new EpisodicRequestError(response.status, reason)
  }
  return response.status === 204 ? null : response.json()
}
export const listEpisodicMemories = (character: string) =>
  request(collection(character)) as Promise<EpisodicMemory[]>
export const correctFact = (character: string, record: EpisodicMemory, value: FiveW, key: string) =>
  request(`${collection(character)}/${record.id}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_version: record.content_version, idempotency_key: key, five_w: value }),
  }) as Promise<EpisodicMemory>
export const deleteFact = async (character: string, record: EpisodicMemory, key: string): Promise<void> => {
  await request(`${collection(character)}/${record.id}`, {
    method: 'DELETE', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_version: record.content_version, idempotency_key: key }),
  })
}
export const emptyTimeParts = (): TimeParts => ({
  year: null, month: null, day: null, hour: null, minute: null, second: null,
})
