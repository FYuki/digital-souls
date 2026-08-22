const API_PREFIX = '/api/characters'

export type PersonaMemory = {
  id: string
  character_id: string
  provider_id: 'core'
  memory_kind: string
  memory_type: string
  normalized_text: string
  structured_value?: Record<string, unknown>
  effective_at: string
  status: string
  content_version: number
  index_pending: boolean
}

export type TemporaryRecord = {
  id: string
  character_id: string
  provider_id: string
  source_ref: string
  record_type: string
  structured_value: string
  effective_at: string
  updated_at: string
}

export class MemoryCorrectionRejected extends Error {
  constructor(readonly reasonCode: string) {
    super(reasonCode)
  }
}

const basePath = (character: string): string => (
  `${API_PREFIX}/${encodeURIComponent(character)}`
)

const requestJson = async (url: string, init?: RequestInit): Promise<unknown> => {
  const response = await fetch(url, init)
  if (!response.ok) {
    if (response.status === 422) {
      const body: unknown = await response.json()
      if (typeof body === 'object' && body !== null && 'reason_code' in body
        && typeof body.reason_code === 'string') {
        throw new MemoryCorrectionRejected(body.reason_code)
      }
    }
    throw new Error(`Memory request failed with status ${response.status}`)
  }
  return response.json()
}

const requestWithoutBody = async (url: string, init: RequestInit): Promise<void> => {
  const response = await fetch(url, init)
  if (!response.ok) throw new Error(`Memory request failed with status ${response.status}`)
}

export const listPersonaMemories = async (character: string): Promise<PersonaMemory[]> => (
  requestJson(`${basePath(character)}/persona-memories?status=ACTIVE`) as Promise<PersonaMemory[]>
)

export const listTemporaryRecords = async (
  character: string,
  provider: string,
): Promise<TemporaryRecord[]> => await requestJson(
  `${basePath(character)}/temporary-records/${provider}`,
) as TemporaryRecord[]

export const correctPersonaMemory = async (
  character: string,
  memory: PersonaMemory,
  structuredValue: Record<string, unknown>,
): Promise<PersonaMemory> => (
  requestJson(`${basePath(character)}/persona-memories/${memory.id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      idempotency_key: crypto.randomUUID(),
      memory_type: memory.memory_type,
      structured_value: structuredValue,
    }),
  }) as Promise<PersonaMemory>
)

export const correctTemporaryRecord = async (
  character: string,
  record: TemporaryRecord,
  correction: Pick<TemporaryRecord, 'record_type' | 'structured_value' | 'effective_at'>,
): Promise<TemporaryRecord> => requestJson(
  `${basePath(character)}/temporary-records/${record.provider_id}/${record.id}`,
  {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      record_type: correction.record_type,
      structured_value: correction.structured_value,
      effective_at: correction.effective_at,
    }),
  },
) as Promise<TemporaryRecord>

export const hardDeleteTemporaryRecord = async (
  character: string,
  record: TemporaryRecord,
): Promise<void> => requestWithoutBody(
  `${basePath(character)}/temporary-records/${record.provider_id}/${record.id}`,
  { method: 'DELETE' },
)

export const hardDeletePersonaMemory = async (
  character: string,
  memoryId: string,
): Promise<void> => requestWithoutBody(
  `${basePath(character)}/persona-memories/${memoryId}`,
  { method: 'DELETE' },
)
