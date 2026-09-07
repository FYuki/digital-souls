export type CoreDeliveryObservation = Readonly<{
  type: 'response_started' | 'response_delta' | 'response_cancelled'
  sessionId: string; responseId: string; generation: number; atMs: number
  duplicate: boolean; textCharacters: number; textSequence: number | null
}>
