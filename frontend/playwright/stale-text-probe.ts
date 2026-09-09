import type {CoreDeliveryObservation} from '../src/livekit/core-delivery-observation'

export type StaleTextChange = Readonly<{
  kind: 'received' | 'dom_added'; lowerMs: number; upperMs: number; characters: number
  duplicate?: boolean; textSequence?: number | null
}>
export type StaleHistoryTextRow = {
  retainedLiveDomTransitions?: number; retainedLiveDomCharacters?: number
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
  const entries = new Map<string, {row: StaleTextRow; lastSampleAtMs: number; lastHistorySampleAtMs: number}>()
  let shown = new WeakMap<HTMLElement, {sessionId: string; responseId: string; text: string; atMs: number; source: 'live' | 'history'}>()
  let closed = false, overflow = false
  function record(row: StaleTextRow | StaleHistoryTextRow, change: StaleTextChange): void {
    if (!Number.isFinite(change.lowerMs) || !Number.isFinite(change.upperMs) || change.lowerMs < 0
      || change.upperMs < change.lowerMs) {row.missingReason = 'text_clock_invalid'; return}
    if (row.changes.length >= 4096) {row.missingReason = 'text_observation_overflow'; overflow = true; return}
    row.changes.push(change)
  }
  function forgetRemovedNodes(records: MutationRecord[]): void {
    for (const record of records) for (const node of record.removedNodes) {
      if (!(node instanceof HTMLElement)) continue
      shown.delete(node)
      for (const child of node.querySelectorAll<HTMLElement>('*')) shown.delete(child)
    }
  }
  function sampleText(owner: StaleTextRow, row: StaleTextRow | StaleHistoryTextRow,
    matches: HTMLElement[], lastSampleAtMs: number, source: 'live' | 'history'): number {
    const now = performance.now()
    if (matches.length > 1) {row.missingReason = 'dom_response_duplicated'; return lastSampleAtMs}
    if (now < lastSampleAtMs) {row.missingReason = 'text_clock_invalid'; return lastSampleAtMs}
    const node = matches[0], next = node?.textContent ?? ''
    if (next.length > 1_000_000) {row.missingReason = 'text_observation_overflow'; return lastSampleAtMs}
    if (node !== undefined) {
      row.domObserved = true
      const candidate = shown.get(node)
      const previous = candidate?.sessionId === owner.sessionId && candidate.responseId === owner.responseId ? candidate : undefined
      let prefix = 0
      while (previous !== undefined && prefix < next.length && prefix < previous.text.length
        && next[prefix] === previous.text[prefix]) prefix++
      if (source === 'history' && previous?.source === 'live') {
        const history = row as StaleHistoryTextRow
        history.retainedLiveDomTransitions = (history.retainedLiveDomTransitions ?? 0) + 1
        history.retainedLiveDomCharacters = (history.retainedLiveDomCharacters ?? 0) + prefix
      }
      const added = next.length - prefix
      if (added > 0) {
        record(row, {kind: 'dom_added', lowerMs: Math.max(0, (previous?.atMs ?? lastSampleAtMs) - 0.2),
          upperMs: now + 0.2, characters: added})
        row.domChanges++; row.domAddedCharacters += added
        if (owner.cancelledAtMs !== null) {row.domAfterCancelChanges++; row.domAfterCancelAddedCharacters += added}
      }
      // 同じDOMが接続したまま継続した場合だけ、既に提示したprefixを次の観測へ引き継ぐ。
      // 別node・一度除去したnode・別応答なら、同文でも全文を新たな提示として数える。
      shown.set(node, {sessionId: owner.sessionId, responseId: owner.responseId, text: next, atMs: now, source})
    }
    row.observedThroughMs = now
    return now
  }
  function sampleDom(): void {
    if (closed) return
    const nodes = [...document.querySelectorAll<HTMLElement>('[data-live-response-text]')]
    const historyNodes = [...document.querySelectorAll<HTMLElement>('[data-history-turn-text]')]
    for (const value of entries.values()) {
      const {row} = value
      if (row.history !== undefined) {
        const history = row.history
        value.lastHistorySampleAtMs = sampleText(row, history,
          historyNodes.filter(node => node.dataset.historyTurnText === history.turnId), value.lastHistorySampleAtMs, 'history')
      }
      value.lastSampleAtMs = sampleText(row, row,
        nodes.filter(node => node.dataset.liveResponseText === row.responseId), value.lastSampleAtMs, 'live')
    }
  }
  const observer = new MutationObserver(records => {forgetRemovedNodes(records); sampleDom()})
  observer.observe(document, {subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['data-live-response-text', 'data-history-turn-text']})
  function flush(): void {forgetRemovedNodes(observer.takeRecords()); sampleDom()}
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
        entry = {lastSampleAtMs: now, lastHistorySampleAtMs: now, row: {sessionId: event.sessionId, responseId: event.responseId,
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
            row.history = {turnId: event.historyTurnId, retainedLiveDomTransitions: 0, retainedLiveDomCharacters: 0, domObserved: false, domChanges: 0, domAddedCharacters: 0,
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
      shown = new WeakMap()
      return snapshot()
    },
  }
}
