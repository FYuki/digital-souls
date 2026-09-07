import type {CoreDeliveryObservation} from '../src/livekit/core-delivery-observation'

export type StaleTextRow = {
  sessionId: string; responseId: string; startedAtMs: number | null; cancelledAtMs: number | null
  receivedEvents: number; receivedCharacters: number; duplicateEvents: number
  receivedAfterCancelEvents: number; receivedAfterCancelCharacters: number
  domObserved: boolean; domChanges: number; domAddedCharacters: number
  domAfterCancelChanges: number; domAfterCancelAddedCharacters: number
  missingReason: 'dom_response_duplicated' | 'text_observation_overflow' | null
}
export type StaleTextSnapshot = {
  scope: 'live_response_dom'; cancelBoundary: 'client_cancel_received'; receiveBoundary: 'validated_before_deduplication'
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
  const entries = new Map<string, {row: StaleTextRow; previousText: string}>()
  let closed = false, overflow = false
  function sampleDom(): void {
    if (closed) return
    const nodes = [...document.querySelectorAll<HTMLElement>('[data-live-response-text]')]
    for (const value of entries.values()) {
      const {row} = value
      const matches = nodes.filter(node => node.dataset.liveResponseText === row.responseId)
      if (matches.length > 1) {row.missingReason = 'dom_response_duplicated'; continue}
      const next = matches[0]?.textContent ?? ''
      if (next.length > 1_000_000) {row.missingReason = 'text_observation_overflow'; continue}
      if (matches.length === 1) row.domObserved = true
      if (next !== value.previousText) {
        // 同じ長さの置換も取りこぼさない。共通prefix以後を新たなDOM提示として保守的に数える。
        let prefix = 0
        while (prefix < next.length && prefix < value.previousText.length && next[prefix] === value.previousText[prefix]) prefix++
        const added = next.length - prefix
        if (added > 0) {
          row.domChanges++; row.domAddedCharacters += added
          if (row.cancelledAtMs !== null) {row.domAfterCancelChanges++; row.domAfterCancelAddedCharacters += added}
        }
        value.previousText = next
      }
    }
  }
  const observer = new MutationObserver(sampleDom)
  observer.observe(document, {subtree: true, childList: true, characterData: true, attributes: true,
    attributeFilter: ['data-live-response-text']})
  function flush(): void {observer.takeRecords(); sampleDom()}
  function snapshot(): StaleTextSnapshot {
    flush()
    return {scope: 'live_response_dom', cancelBoundary: 'client_cancel_received',
      receiveBoundary: 'validated_before_deduplication', characterUnit: 'utf16_code_units',
      closed, overflow, observedAtMs: performance.now(), rows: [...entries.values()].map(({row}) => ({...row}))}
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
        entry = {previousText: '', row: {sessionId: event.sessionId, responseId: event.responseId,
          startedAtMs: null, cancelledAtMs: null, receivedEvents: 0, receivedCharacters: 0, duplicateEvents: 0,
          receivedAfterCancelEvents: 0, receivedAfterCancelCharacters: 0,
          domObserved: false, domChanges: 0, domAddedCharacters: 0, domAfterCancelChanges: 0,
          domAfterCancelAddedCharacters: 0, missingReason: null}}
        entries.set(key, entry)
      }
      const row = entry.row
      if (event.type === 'response_started' && row.startedAtMs === null) row.startedAtMs = event.atMs
      if (event.type === 'response_cancelled' && row.cancelledAtMs === null) row.cancelledAtMs = event.atMs
      if (event.type === 'response_delta') {
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
      for (const entry of entries.values()) entry.previousText = ''
      return snapshot()
    },
  }
}
