export type PreparationObservation = Readonly<{
  method: 'browser_click_to_microphone_standby_dom_v1'
  clock_domain: 'browser_performance'
  attempt_count: number
  started_client_ms: number | null
  ready_client_ms: number | null
  duration_ms: number | null
}>

declare global {
  interface Window {
    __voicePreparationProbe?: {
      snapshot: () => PreparationObservation
      close: () => void
    }
  }
}

// addInitScriptで直列化するため、この関数は実行時の外部依存を持たない。
export const installPreparationProbe = (): void => {
  let attempts = 0
  let started: number | null = null
  let ready: number | null = null
  const selector = 'button[aria-label^="マイクを"]'
  const observeReady = () => {
    const button = document.querySelector<HTMLButtonElement>(selector)
    if (started !== null && ready === null && button?.getAttribute('aria-pressed') === 'true'
      && button.classList.contains('mic-standby')) ready = performance.now()
  }
  const click = (event: MouseEvent) => {
    const button = event.target instanceof Element ? event.target.closest<HTMLButtonElement>(selector) : null
    if (!button || button.disabled || button.getAttribute('aria-pressed') !== 'false') return
    attempts += 1
    // 再試行で最初の失敗を上書きしない。複数操作は別途invalidとして集計する。
    if (started === null) started = performance.now()
  }
  const observer = new MutationObserver(observeReady)
  const attach = () => {
    if (document.documentElement) observer.observe(document.documentElement, {
      subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'aria-pressed'],
    })
  }
  document.addEventListener('click', click, true)
  if (document.documentElement) attach()
  else document.addEventListener('DOMContentLoaded', attach, {once: true})
  window.__voicePreparationProbe = {
    snapshot: () => ({
      method: 'browser_click_to_microphone_standby_dom_v1', clock_domain: 'browser_performance',
      attempt_count: attempts, started_client_ms: started, ready_client_ms: ready,
      duration_ms: started !== null && ready !== null ? ready - started : null,
    }),
    close: () => {
      observer.disconnect()
      document.removeEventListener('click', click, true)
      document.removeEventListener('DOMContentLoaded', attach)
    },
  }
}
