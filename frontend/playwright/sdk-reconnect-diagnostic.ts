import type {Page} from '@playwright/test'

// SDKの全文・context・URL・認証情報を保存せず、終了通知の固定形式だけを採る。
export function classifySdkReconnectLog(text: string) {
  const match = /^could not recover connection after ([0-9]+) attempts, (-?[0-9]+)ms\. giving up(?:$| )/.exec(text)
  if (!match) return null
  const attempts = Number(match[1]), elapsedMs = Number(match[2])
  if (!Number.isSafeInteger(attempts) || !Number.isSafeInteger(elapsedMs)) return null
  return {stage: 'gave_up' as const, attempts, elapsedMs}
}

export async function installSdkReconnectDiagnostic(page: Page) {
  const record = {clock_domain: 'playwright_monotonic', overflow: false,
    events: [] as Array<NonNullable<ReturnType<typeof classifySdkReconnectLog>> & {at_ms: number}>}
  page.on('console', message => {
    const event = classifySdkReconnectLog(message.text())
    if (!event) return
    if (record.events.length >= 200) {record.overflow = true; return}
    record.events.push({...event, at_ms: performance.now()})
  })
  await page.addInitScript(() => {
    const target = window as typeof window & {__voiceSdkClockDiagnostic?: {
      events: Array<{at_ms: number; wall_minus_monotonic_delta_ms: number}>;
      overflow: boolean; close: () => void}}
    let wall = Date.now(), monotonic = performance.now()
    const state = {events: [] as Array<{at_ms: number; wall_minus_monotonic_delta_ms: number}>,
      overflow: false, close: () => clearInterval(timer)}
    const timer = setInterval(() => {
      const nextWall = Date.now(), nextMonotonic = performance.now()
      const delta = nextWall - wall - (nextMonotonic - monotonic)
      // 絶対日時・時刻起点は保存しない。10msを超える時計間差の変化だけを記録する。
      if (Math.abs(delta) > 10) {
        if (state.events.length < 200) state.events.push({at_ms: nextMonotonic, wall_minus_monotonic_delta_ms: delta})
        else state.overflow = true
      }
      wall = nextWall; monotonic = nextMonotonic
    }, 100)
    target.__voiceSdkClockDiagnostic = state
  })
  return record
}

export async function finishSdkClockDiagnostic(page: Page) {
  return page.evaluate(() => {
    const state = (window as typeof window & {__voiceSdkClockDiagnostic?: {
      events: Array<{at_ms: number; wall_minus_monotonic_delta_ms: number}>;
      overflow: boolean; close: () => void}}).__voiceSdkClockDiagnostic
    if (!state) return {status: 'unavailable'}
    state.close()
    return {status: 'captured', clock_domain: 'browser_monotonic', events: state.events, overflow: state.overflow}
  }).catch(() => ({status: 'unavailable'}))
}
