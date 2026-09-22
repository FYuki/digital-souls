import { requestJson, requestVoid } from '../api/http'
import { characterApiPath } from '../api/paths'
import { isRecord } from '../validation/primitives'

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

const REQUEST_LABEL = 'Memory request'

const basePath = (character: string): string => characterApiPath(character)

// 422の訂正拒否だけdomain例外へ変換し、他は共有のstatusエラーを使う。
const mapError = async (response: Response): Promise<Error | null> => {
  if (response.status !== 422) return null
  const body: unknown = await response.json()
  if (isRecord(body) && typeof body.reason_code === 'string') {
    return new MemoryCorrectionRejected(body.reason_code)
  }
  return null
}

const requestMemoryJson = (url: string, init?: RequestInit): Promise<unknown> => (
  requestJson(url, REQUEST_LABEL, init, mapError)
)

export const listPersonaMemories = async (character: string): Promise<PersonaMemory[]> => (
  requestMemoryJson(`${basePath(character)}/persona-memories?status=ACTIVE`) as Promise<PersonaMemory[]>
)

export const listTemporaryRecords = async (
  character: string,
  provider: string,
): Promise<TemporaryRecord[]> => await requestMemoryJson(
  `${basePath(character)}/temporary-records/${provider}`,
) as TemporaryRecord[]

export const correctPersonaMemory = async (
  character: string,
  memory: PersonaMemory,
  structuredValue: Record<string, unknown>,
  idempotencyKey: string,
): Promise<PersonaMemory> => (
  requestMemoryJson(`${basePath(character)}/persona-memories/${memory.id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      idempotency_key: idempotencyKey,
      memory_type: memory.memory_type,
      structured_value: structuredValue,
    }),
  }) as Promise<PersonaMemory>
)

export const correctTemporaryRecord = async (
  character: string,
  record: TemporaryRecord,
  correction: Pick<TemporaryRecord, 'record_type' | 'structured_value' | 'effective_at'>,
): Promise<TemporaryRecord> => requestMemoryJson(
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
): Promise<void> => requestVoid(
  `${basePath(character)}/temporary-records/${record.provider_id}/${record.id}`,
  { method: 'DELETE' },
  REQUEST_LABEL,
)

export const hardDeletePersonaMemory = async (
  character: string,
  memoryId: string,
): Promise<void> => requestVoid(
  `${basePath(character)}/persona-memories/${memoryId}`,
  { method: 'DELETE' },
  REQUEST_LABEL,
)
