// BEの正式な発話区間だけを照合する。通知時刻を音声sourceの時刻へ読み替えない。
export type CoreDiagnostic = Record<string, string | number | boolean | null>
export type BackendVadObservation = {
  status: 'observed' | 'missing'
  reason: string | null
  confirmed: number
  ended: number
  finalized: number
  utteranceIds: string[]
}
export type BackendVadInput = {
  events: readonly CoreDiagnostic[]
  overflow: boolean
  sessionId: string
  initialUtteranceId: string
  sourceStartLowerMs: number
}

export function readBackendVadObservation(input: BackendVadInput): BackendVadObservation {
  const result: BackendVadObservation = {
    status: 'missing', reason: null, confirmed: 0, ended: 0, finalized: 0, utteranceIds: [],
  }
  const missing = (reason: string): BackendVadObservation => ({...result, reason})
  const integer = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
  const timestamp = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0
  const boundary = (row: CoreDiagnostic) =>
    row.sessionId === input.sessionId
    && typeof row.utteranceId === 'string' && row.utteranceId.length > 0
    && typeof row.trackSid === 'string' && /^TR_[A-Za-z0-9_-]{1,100}$/.test(row.trackSid)
    && integer(row.inputGeneration) && row.inputGeneration > 0
    && row.sampleRate === 16000 && row.clockDomain === 'server_monotonic'
    && timestamp(row.atMs) && timestamp(row.serverTimestampMs)
    && integer(row.startSample) && integer(row.activeEndSample) && integer(row.detectedSample)
    && row.startSample <= row.activeEndSample && row.activeEndSample <= row.detectedSample

  if (input.overflow) return missing('backend_event_overflow')
  if (!timestamp(input.sourceStartLowerMs)) return missing('fixture_boundary_unavailable')
  const initial = input.events.filter(row => row.type === 'speech_started' && row.utteranceId === input.initialUtteranceId)
  if (initial.length !== 1 || !boundary(initial[0])) return missing('initial_input_identity_unavailable')
  const grant = initial[0]
  if (input.events.some(row => row.utteranceId !== input.initialUtteranceId
    && ['speech_started', 'speech_stopped'].includes(String(row.type)) && !timestamp(row.atMs))) {
    return missing('backend_boundary_order_invalid')
  }
  const rows = input.events.filter(row => row.utteranceId !== input.initialUtteranceId
    && timestamp(row.atMs) && row.atMs >= input.sourceStartLowerMs)
  if (rows.some(row => row.type === 'utterance_discarded'
    || (row.type === 'error' && ['audio_input_repeat_required', 'audio_input_unavailable'].includes(String(row.reasonCode))))) {
    return missing('backend_input_discarded')
  }
  const starts = rows.filter(row => row.type === 'speech_started')
  const stops = rows.filter(row => row.type === 'speech_stopped')
  result.confirmed = starts.length
  result.ended = stops.length
  if (starts.length === 0) return missing('speech_not_confirmed')
  if ([...starts, ...stops].some(row => !boundary(row)
    || row.trackSid !== grant.trackSid || row.inputGeneration !== grant.inputGeneration)) {
    return missing('backend_input_identity_mismatch')
  }
  const ids = starts.map(row => String(row.utteranceId))
  result.utteranceIds = ids
  if (new Set(ids).size !== ids.length) return missing('backend_boundary_duplicated')
  if (stops.some(row => !ids.includes(String(row.utteranceId)))) return missing('backend_boundary_unpaired')
  for (const start of starts) {
    const matched = stops.filter(row => row.utteranceId === start.utteranceId)
    if (matched.length === 0) return missing('speech_end_unavailable')
    if (matched.length !== 1) return missing('backend_boundary_duplicated')
    const end = matched[0]
    if (end.startSample !== start.startSample
      || Number(end.detectedSample) < Number(start.detectedSample)
      || Number(end.activeEndSample) < Number(start.activeEndSample)
      || Number(end.serverTimestampMs) < Number(start.serverTimestampMs)) {
      return missing('backend_boundary_order_invalid')
    }
    const finalized = rows.filter(row => row.type === 'utterance_finalized'
      && row.sessionId === input.sessionId && row.utteranceId === start.utteranceId)
    if (finalized.length > 1) return missing('backend_finalization_duplicated')
    if (finalized.length === 1) result.finalized += 1
  }
  const ordered = [...starts].sort((a, b) => Number(a.startSample) - Number(b.startSample))
  for (let index = 1; index < ordered.length; ++index) {
    const previous = stops.find(row => row.utteranceId === ordered[index - 1].utteranceId)!
    if (Number(ordered[index].startSample) < Number(previous.detectedSample)) {
      return missing('backend_boundary_order_invalid')
    }
  }
  if (result.finalized !== starts.length) return missing('final_utterance_unavailable')
  return {...result, status: 'observed', reason: null}
}
