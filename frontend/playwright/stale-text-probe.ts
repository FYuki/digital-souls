import type {CoreDeliveryObservation} from '../src/livekit/core-delivery-observation'

export type StaleTextChange = Readonly<{
  kind: 'received' | 'dom_added'; lowerMs: number; upperMs: number; characters: number
  duplicate?: boolean; textSequence?: number | null
}>
export type StaleHistoryTextRow = {
  turnId: string; domObserved: boolean; domChanges: number; domAddedCharacters: number
  domAfterCancelChanges: number; domAfterCancelAddedCharacters: number
  observedFromMs: number; observedThroughMs: number; changes: StaleTextChange[]
  missingReason: 'history_mapping_conflict' | 'dom_response_duplicated' | 'text_clock_invalid' | 'text_observation_overflow' | null
}
export type StaleTextRow = {
  history?: StaleHistoryTextRow
  sessionId: string; responseId: string; startedAtMs: number | null; cancelledAtMs: number | null
  receivedEvents: number; receivedCharacters: number; duplicateEvents: number
  receivedAfterCancelEvents: number; receivedAfterCancelCharacters: number
  domObserved: boolean; domChanges: number; domAddedCharacters: number
  domAfterCancelChanges: number; domAfterCancelAddedCharacters: number
  observedFromMs: number; observedThroughMs: number; changes: StaleTextChange[]
  missingReason: 'text_clock_invalid' | 'dom_response_duplicated' | 'text_observation_overflow' | null
}
export type StaleTextSnapshot = {
  scope: 'live_and_history_response_dom'; cancelBoundary: 'client_cancel_received'; receiveBoundary: 'validated_before_deduplication'
  characterUnit: 'utf16_code_units'; closed: boolean; overflow: boolean; observedAtMs: number; rows: StaleTextRow[]
}
declare global {
  interface Window {
    __voiceStaleTextProbe?: {
      receive: (row: CoreDeliveryObservation) => void
      snapshot: () => StaleTextSnapshot
      close: () => StaleTextSnapshot
    }
  }
}

// addInitScriptで単独実行する。本文は同一ページ内の比較にだけ使い、記録には数値しか出さない。
export function installStaleTextProbe(): void {
  const entries = new Map<string, {row: StaleTextRow; previousText: string; lastSampleAtMs: number; previousHistoryText: string; lastHistorySampleAtMs: number}>()
  let closed = false, overflow = false
  function record(row: StaleTextRow | StaleHistoryTextRow, change: StaleTextChange): void {
    if (!Number.isFinite(change.lowerMs) || !Number.isFinite(change.upperMs) || change.lowerMs < 0
      || change.upperMs < change.lowerMs) {row.missingReason = 'text_clock_invalid'; return}
    if (row.changes.length >= 4096) {row.missingReason = 'text_observation_overflow'; overflow = true; return}
    row.changes.push(change)
  }
  function sampleDom(): void {
    if (closed) return
    const nodes = [...document.querySelectorAll<HTMLElement>('[data-live-response-text]')]
    const historyNodes = [...document.querySelectorAll<HTMLElement>('[data-history-turn-text]')]
    for (const value of entries.values()) {
      const {row} = value
      const history = row.history
      if (history !== undefined) {
        const matches = historyNodes.filter(node => node.dataset.historyTurnText === history.turnId)
        const now = performance.now()
        if (matches.length > 1) history.missingReason = 'dom_response_duplicated'
        else if (now < value.lastHistorySampleAtMs) history.missingReason = 'text_clock_invalid'
        else {
          const next = matches[0]?.textContent ?? ''
          if (next.length > 1_000_000) history.missingReason = 'text_observation_overflow'
          else {
            if (matches.length === 1) history.domObserved = true
            let prefix = 0
            while (prefix < next.length && prefix < value.previousHistoryText.length
              && next[prefix] === value.previousHistoryText[prefix]) prefix++
            const added = next.length - prefix
            if (added > 0) {
              // ライブ表示から履歴へ同じ文章を再提示した場合も、省略せず別チャネルへ記録する。
              record(history, {kind: 'dom_added', lowerMs: Math.max(0, value.lastHistorySampleAtMs - 0.2),
                upperMs: now + 0.2, characters: added})
              history.domChanges++; history.domAddedCharacters += added
              if (row.cancelledAtMs !== null) {history.domAfterCancelChanges++; history.domAfterCancelAddedCharacters += added}
            }
            value.previousHistoryText = next
          }
          value.lastHistorySampleAtMs = now; history.observedThroughMs = now
        }
      }
      const matches = nodes.filter(node => node.dataset.liveResponseText === row.responseId)
      if (matches.length > 1) {row.missingReason = 'dom_response_duplicated'; continue}
      const next = matches[0]?.textContent ?? ''
      const sampledAtMs = performance.now()
      if (sampledAtMs < value.lastSampleAtMs) {row.missingReason = 'text_clock_invalid'; continue}
      if (next.length > 1_000_000) {row.missingReason = 'text_observation_overflow'; continue}
      if (matches.length === 1) row.domObserved = true
      if (next !== value.previousText) {
        // 同じ長さの置換も取りこぼさない。共通prefix以後を新たなDOM提示として保守的に数える。
        let prefix = 0
        while (prefix < next.length && prefix < value.previousText.length && next[prefix] === value.previousText[prefix]) prefix++
        const added = next.length - prefix
        if (added > 0) {
          record(row, {kind: 'dom_added', lowerMs: Math.max(0, value.lastSampleAtMs - 0.2),
            upperMs: sampledAtMs + 0.2, characters: added})
          row.domChanges++; row.domAddedCharacters += added
          if (row.cancelledAtMs !== null) {row.domAfterCancelChanges++; row.domAfterCancelAddedCharacters += added}
        }
        value.previousText = next
      }
      value.lastSampleAtMs = sampledAtMs; row.observedThroughMs = sampledAtMs
    }
  }
  const observer = new MutationObserver(sampleDom)
  observer.observe(document, {subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['data-live-response-text', 'data-history-turn-text']})
  function flush(): void {observer.takeRecords(); sampleDom()}
  function snapshot(): StaleTextSnapshot {
    flush()
    return {scope: 'live_and_history_response_dom', cancelBoundary: 'client_cancel_received',
      receiveBoundary: 'validated_before_deduplication', characterUnit: 'utf16_code_units',
      closed, overflow, observedAtMs: performance.now(), rows: [...entries.values()].map(({row}) => ({...row, changes: row.changes.map(change => ({...change})),
        ...(row.history === undefined ? {} : {history: {...row.history, changes: row.history.changes.map(change => ({...change}))}})}))}
  }
  window.__voiceStaleTextProbe = {
    receive(event) {
      if (closed) return
      // cancel前に既にDOMへ反映された文字を、遅延したMutationObserver通知でstaleへ数えない。
      flush()
      const key = `${event.sessionId}:${event.responseId}`
      let entry = entries.get(key)
      if (!entry) {
        if (entries.size >= 128) {overflow = true; return}
        const now = performance.now()
        entry = {previousText: '', lastSampleAtMs: now, previousHistoryText: '', lastHistorySampleAtMs: now, row: {sessionId: event.sessionId, responseId: event.responseId,
          startedAtMs: null, cancelledAtMs: null, receivedEvents: 0, receivedCharacters: 0, duplicateEvents: 0,
          receivedAfterCancelEvents: 0, receivedAfterCancelCharacters: 0,
          domObserved: false, domChanges: 0, domAddedCharacters: 0, domAfterCancelChanges: 0,
          domAfterCancelAddedCharacters: 0, missingReason: null, observedFromMs: now, observedThroughMs: now, changes: []}}
        entries.set(key, entry)
      }
      const row = entry.row
      if (event.type === 'response_started') {
        if (row.startedAtMs === null) row.startedAtMs = event.atMs
        if (event.historyTurnId !== undefined) {
          if (row.history === undefined) {
            const now = performance.now()
            entry.lastHistorySampleAtMs = now
            row.history = {turnId: event.historyTurnId, domObserved: false, domChanges: 0, domAddedCharacters: 0,
              domAfterCancelChanges: 0, domAfterCancelAddedCharacters: 0,
              observedFromMs: now, observedThroughMs: now, changes: [], missingReason: null}
          } else if (row.history.turnId !== event.historyTurnId) row.history.missingReason = 'history_mapping_conflict'
          for (const other of entries.values()) {
            if (other !== entry && other.row.history?.turnId === event.historyTurnId) {
              row.history.missingReason = 'history_mapping_conflict'
              other.row.history.missingReason = 'history_mapping_conflict'
            }
          }
        }
      }
      if (event.type === 'response_cancelled' && row.cancelledAtMs === null) row.cancelledAtMs = event.atMs
      if (event.type === 'response_delta') {
        record(row, {kind: 'received', lowerMs: Math.max(0, event.atMs - 0.2), upperMs: event.atMs + 0.2,
          characters: event.textCharacters, duplicate: event.duplicate, textSequence: event.textSequence})
        row.receivedEvents++; row.receivedCharacters += event.textCharacters
        if (event.duplicate) row.duplicateEvents++
        if (row.cancelledAtMs !== null) {
          row.receivedAfterCancelEvents++; row.receivedAfterCancelCharacters += event.textCharacters
        }
      }
    },
    snapshot,
    close() {
      flush(); observer.disconnect(); closed = true
      for (const entry of entries.values()) {entry.previousText = ''; entry.previousHistoryText = ''}
      return snapshot()
    },
  }
}
