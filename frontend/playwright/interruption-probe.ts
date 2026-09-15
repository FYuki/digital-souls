import type {RoomObservation} from '../src/livekit/room'

export type InterruptionEvidence = {
  responseId: string
  utteranceId: string | null
  // 旧FE VAD経路の観測値だけを残す。BE検知・判定受信時刻で補完しない。
  speechStartedAtMs: number | null
  backendDecisionReceivedAtMs: number | null
  localPlaybackStoppedAtMs: number
  cancelConfirmedAtMs: number | null
  ambiguousDecision: boolean
  duplicateStop: boolean
}

type DecisionEvent = {
  type: string; decision?: string; response_id?: string; utterance_id?: string
}

declare global {
  interface Window {
    __voiceInterruptionProbe?: {
      observeRoom: (observation: Pick<RoomObservation,
        'activeResponseId' | 'localPlaybackStoppedAtMs' | 'cancelConfirmedAtMs' | 'speechStartedAtMs'>) => void
      receiveCoreEvent: (event: DecisionEvent) => void
    }
  }
}

// addInitScriptへ渡すため、実行時のimportや外側の変数に依存しない。
export function installInterruptionProbe(): void {
  type Pending = {
    row?: InterruptionEvidence
    utteranceId: string | null
    decisionAtMs: number | null
    cancelledAtMs: number | null
    ambiguous: boolean
  }
  const pending = new Map<string, Pending>()
  const entry = (responseId: string): Pending | undefined => {
    const existing = pending.get(responseId)
    if (existing) return existing
    if (pending.size >= 1024) {
      window.__voiceChatE2E.interruptionsOverflow = true
      return undefined
    }
    const value: Pending = {
      utteranceId: null, decisionAtMs: null, cancelledAtMs: null, ambiguous: false,
    }
    pending.set(responseId, value)
    return value
  }
  window.__voiceInterruptionProbe = {
    observeRoom: observation => {
      const responseId = observation.activeResponseId
      if (!responseId || (observation.localPlaybackStoppedAtMs === undefined
        && observation.cancelConfirmedAtMs === undefined)) return
      const value = entry(responseId)
      if (!value) return
      if (observation.cancelConfirmedAtMs !== undefined) {
        value.cancelledAtMs ??= observation.cancelConfirmedAtMs
      }
      if (observation.localPlaybackStoppedAtMs !== undefined) {
        if (!value.row) {
          value.row = {
            responseId, utteranceId: value.utteranceId,
            speechStartedAtMs: observation.speechStartedAtMs ?? null,
            backendDecisionReceivedAtMs: value.decisionAtMs,
            localPlaybackStoppedAtMs: observation.localPlaybackStoppedAtMs,
            cancelConfirmedAtMs: value.cancelledAtMs,
            ambiguousDecision: value.ambiguous, duplicateStop: false,
          }
          window.__voiceChatE2E.interruptions.push(value.row)
        } else if (value.row.localPlaybackStoppedAtMs !== observation.localPlaybackStoppedAtMs) {
          value.row.duplicateStop = true
        }
      }
      if (value.row) value.row.cancelConfirmedAtMs = value.cancelledAtMs
    },
    receiveCoreEvent: event => {
      if (event.type !== 'turn_decision' || event.decision !== 'take_turn'
        || !event.response_id || !event.utterance_id) return
      const value = entry(event.response_id)
      if (!value) return
      if (value.utteranceId !== null && value.utteranceId !== event.utterance_id) {
        value.ambiguous = true
      } else {
        value.utteranceId = event.utterance_id
        value.decisionAtMs ??= performance.now()
      }
      if (value.row) {
        value.row.utteranceId = value.utteranceId
        value.row.backendDecisionReceivedAtMs = value.decisionAtMs
        value.row.ambiguousDecision = value.ambiguous
      }
    },
  }
}
