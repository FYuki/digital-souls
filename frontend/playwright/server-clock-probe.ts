import type {ControlProbeObservation} from '../src/livekit/control-probe'

export type ServerClockSnapshot = {
  method: 'causal_probe_brackets_v1'; periodMs: number; closed: boolean; overflow: boolean
  observations: ControlProbeObservation[]
}
declare global {
  interface Window {
    __voiceServerClockProbe?: {snapshot: () => ServerClockSnapshot; close: () => ServerClockSnapshot}
  }
}

// 実接続の往復を直列に観測する。接続フラグや送信完了を相手側の受付へ補完しない。
export function installServerClockProbe(): void {
  const observations: ControlProbeObservation[] = []
  let closed = false, overflow = false, timer: ReturnType<typeof setTimeout> | undefined
  const periodMs = 20
  const snapshot = (): ServerClockSnapshot => ({method: 'causal_probe_brackets_v1', periodMs, closed, overflow,
    observations: observations.map(row => ({...row}))})
  window.__voiceServerClockProbe = {snapshot, close() {
    closed = true; clearTimeout(timer); return snapshot()
  }}
  const target = window as typeof window & {__digitalSoulsVoiceSessionTestPort?: {
    bindClockProbe?: (probe: () => Promise<ControlProbeObservation>) => void}}
  target.__digitalSoulsVoiceSessionTestPort ??= {}
  target.__digitalSoulsVoiceSessionTestPort.bindClockProbe = probe => {
    async function sample(): Promise<void> {
      if (closed) return
      const row = await probe()
      if (closed) return
      if (observations.length >= 2048) {overflow = true; closed = true; return}
      observations.push(row)
      timer = setTimeout(() => {void sample()}, periodMs)
    }
    void sample()
  }
}
