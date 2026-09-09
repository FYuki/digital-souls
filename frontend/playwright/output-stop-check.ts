import {postGainAuditSource} from '../src/livekit/post-gain-audit'
import {PostGainAudioMonitor, type PostGainWorkletMessage, type StaleAudioObservation} from '../src/livekit/post-gain-monitor'

// 実AudioContext・worklet・出力時計を使う。ネットワーク音声やCore取消全体の受け入れではない。
export async function checkBrowserOutputStop() {
  const context = new AudioContext({sampleRate: 48000, latencyHint: 0})
  const moduleUrl = URL.createObjectURL(new Blob([postGainAuditSource], {type: 'text/javascript'}))
  let monitor: PostGainAudioMonitor | null = null
  let oscillator: OscillatorNode | null = null
  let oscillatorStarted = false
  let timer: ReturnType<typeof setTimeout> | null = null
  const rows: StaleAudioObservation[] = []
  try {
    await context.audioWorklet.addModule(moduleUrl)
    monitor = new PostGainAudioMonitor(context, 'synthetic-response', 'synthetic-session', 0, row => rows.push(row))
    const observedSignal = new Promise<void>((resolve, reject) => {
      timer = setTimeout(() => reject(new Error('initial_output_not_observed')), 3000)
      const listen = (event: MessageEvent<PostGainWorkletMessage>) => {
        if (event.data.kind === 'output' && event.data.intervals.some(interval => interval.nonzeroSamples > 0)) {
          monitor!.node.port.removeEventListener('message', listen)
          if (timer !== null) {clearTimeout(timer); timer = null}
          resolve()
        }
      }
      monitor!.node.port.addEventListener('message', listen)
    })
    oscillator = context.createOscillator()
    const gain = context.createGain(); gain.gain.value = .05
    oscillator.connect(gain); gain.connect(monitor.node)
    await context.resume()
    oscillator.start(context.currentTime + .05)
    oscillatorStarted = true
    await observedSignal
    const requestedAt = performance.now()
    const confirmation = await monitor.stopAndConfirm()
    const elapsedMs = performance.now() - requestedAt
    // sourceを動かしたまま最終出力段の停止を検証し、その後の独立監視で残留を数える。
    monitor.cancel(performance.now())
    await monitor.dispose()
    const final = rows.at(-1)
    if (final === undefined) throw new Error('output_stop_audit_missing')
    return {elapsedMs, outputClockPassedStop: confirmation.outputClockPassedFrame >= confirmation.endFrame,
      graphClosed: final.graphClosed, auditComplete: final.audit.complete, drained: final.audit.drained,
      missingReason: final.audit.missingReason, staleSamplesUpper: final.audit.nonzeroSamplesAfterCancelUpper,
      stopConfirmationRecorded: final.outputStopConfirmation?.endFrame === confirmation.endFrame}
  } finally {
    if (timer !== null) clearTimeout(timer)
    if (oscillatorStarted) oscillator?.stop()
    oscillator?.disconnect()
    await monitor?.dispose()
    await context.close()
    URL.revokeObjectURL(moduleUrl)
  }
}
