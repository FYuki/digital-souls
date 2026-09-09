// ページの操作activationを観測する。キーボードによるbutton activationもclickになる。
// 開始操作の完了後から後片付け前まで数え、対象の文言・入力本文は保存しない。
export type UserControlObservation = {
  method: 'document_activation_clicks_v1'
  startedAtMs: number
  observedAtMs: number
  activationTimesMs: number[]
}

declare global {
  interface Window {
    __voiceUserControlProbe?: {
      begin: () => void
      snapshot: () => UserControlObservation
    }
  }
}

// addInitScriptでブラウザへ渡すため、外部変数に依存しない。
export function installUserControlProbe(): void {
  let startedAtMs: number | undefined
  const activationTimesMs: number[] = []
  document.addEventListener('click', () => {
    if (startedAtMs !== undefined) activationTimesMs.push(performance.now())
  }, true)
  window.__voiceUserControlProbe = {
    begin() {
      if (startedAtMs !== undefined) throw new Error('user control observation already started')
      startedAtMs = performance.now()
    },
    snapshot() {
      if (startedAtMs === undefined) throw new Error('user control observation not started')
      return { method: 'document_activation_clicks_v1', startedAtMs,
        observedAtMs: performance.now(), activationTimesMs: [...activationTimesMs] }
    },
  }
}
