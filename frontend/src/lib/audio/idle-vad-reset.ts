// MicVADの公開APIだけで、長い静音のモデル状態を区切る。
// 受け取ったframeは保持してreset後に処理し、入力PCMを切り落とさない。
export type ResettableVad = {
  processFrame: (frame: Float32Array) => Promise<void>
  pause: () => Promise<void>
  start: () => Promise<void>
}
export const idleVadResetOptions = {sampleRate: 16000, maximumRms: 0.001, minimumQuietMs: 700, intervalMs: 256} as const

export function attachIdleVadReset(vad: ResettableVad, onError: (error: unknown) => void): {close: () => Promise<void>} {
  const process = vad.processFrame.bind(vad)
  let closed = false
  let quietMs = 0
  let elapsedSinceReset = Number(idleVadResetOptions.intervalMs)
  let pending = Promise.resolve()
  let queuedFrames = 0
  vad.processFrame = frame => {
    if (closed) return Promise.resolve()
    if (queuedFrames >= 16) {
      closed = true
      onError(new Error('VAD frame processing backlog'))
      return pending
    }
    queuedFrames++
    const samples = frame.slice()
    pending = pending.then(async () => {
      if (closed) return
      let energy = 0
      for (const sample of samples) energy += sample * sample
      const duration = samples.length * 1000 / idleVadResetOptions.sampleRate
      const quiet = samples.length > 0 && Number.isFinite(energy)
        && Math.sqrt(energy / samples.length) < idleVadResetOptions.maximumRms
      quietMs = quiet ? Math.min(quietMs + duration, idleVadResetOptions.minimumQuietMs) : 0
      elapsedSinceReset = Math.min(elapsedSinceReset + duration, idleVadResetOptions.intervalMs)
      if (quietMs >= idleVadResetOptions.minimumQuietMs && elapsedSinceReset >= idleVadResetOptions.intervalMs) {
        await vad.pause()
        if (closed) return
        await vad.start()
        elapsedSinceReset = 0
      }
      if (!closed) await process(samples)
    }).catch(error => {closed = true; onError(error)}).finally(() => {queuedFrames--})
    return pending
  }
  return {close: () => {closed = true; return pending}}
}
