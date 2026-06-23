const CHAT_ENDPOINT = '/api/chat'
const MIORI_CHARACTER = 'miori'
const JSON_CONTENT_TYPE = 'application/json'

type ChatResponse = {
  character: typeof MIORI_CHARACTER
  response: string
}

const isChatResponse = (value: unknown): value is ChatResponse => {
  if (typeof value !== 'object' || value === null) {
    return false
  }

  const record = value as Record<string, unknown>
  return record.character === MIORI_CHARACTER && typeof record.response === 'string'
}

export const sendMessage = async (message: string): Promise<string> => {
  const response = await fetch(CHAT_ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': JSON_CONTENT_TYPE },
    body: JSON.stringify({ character: MIORI_CHARACTER, message }),
  })

  if (!response.ok) {
    throw new Error(`Chat request failed with status ${response.status}`)
  }

  const data: unknown = await response.json()

  if (!isChatResponse(data)) {
    throw new Error('Chat response shape is invalid')
  }

  return data.response
}
