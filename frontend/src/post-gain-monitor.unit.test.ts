import {afterEach, beforeEach, expect, test, vi} from 'vitest'
import {PostGainAudioMonitor, type StaleAudioObservation, type PostGainWorkletMessage} from './livekit/post-gain-monitor'

class FakeNode {
  port = {onmessage: null as ((event: MessageEvent<PostGainWorkletMessage>) => void) | null,
    postMessage: vi.fn(), close: vi.fn()}
  connect = vi.fn()
  disconnect = vi.fn()
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('AudioWorkletNode', FakeNode)
})
afterEach(() => {vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals()})
function fixture(latencies: {baseLatency?: number; outputLatency?: number} = {}, diagnostic = true) {
  let now = 1000
  let clock: AudioTimestamp = {contextTime: 0, performanceTime: 0}
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const context = {sampleRate: 48000, ...latencies, destination: {}, getOutputTimestamp: () => clock} as AudioContext
  const rows: StaleAudioObservation[] = []
  const monitor = new PostGainAudioMonitor(context, 'response', 'session', 3, diagnostic ? row => rows.push(row) : undefined)
  const node = monitor.node as unknown as FakeNode
  const send = (data: PostGainWorkletMessage) => node.port.onmessage?.({data} as MessageEvent<PostGainWorkletMessage>)
  const output = () => {
    clock = {contextTime: 1.009, performanceTime: 1009}; now = 1009.5
    send({kind: 'output', confirmedFrame: 49920, intervals: Array.from({length: 16}, (_, i) => ({
      startFrame: 48000 + i * 128, endFrame: 48128 + i * 128,
      nonzeroSamples: i === 0 ? 128 : 0, firstNonzeroFrame: i === 0 ? 48000 : null, lastNonzeroFrame: i === 0 ? 48127 : null,
    }))})
  }
  const finish = async () => {
    clock = {contextTime: 1.012, performanceTime: 1012}; now = 1012.5
    await vi.advanceTimersByTimeAsync(5)
    clock = {contextTime: 1.05, performanceTime: 1050}; now = 1051
    send({kind: 'finished', endFrame: 50048})
  }
  return {monitor, node, rows, send, output, finish, now: (n: number) => {now = n},
    clock: (value: AudioTimestamp) => {clock = value}}
}

test('cancel後もnodeと出力時計の観測を保持し、drain後のdisposeで閉じる', async () => {
  const f = fixture(); f.output(); f.now(1010); f.monitor.cancel(1010)
  expect(f.node.disconnect).not.toHaveBeenCalled()
  expect(f.node.port.postMessage).toHaveBeenCalledWith({kind: 'finish'})
  expect(f.rows.at(-1)?.audit.complete).toBe(false)
  f.monitor.received(960)
  const disposed = f.monitor.dispose()
  expect(f.node.port.close).not.toHaveBeenCalled()
  await f.finish()
  await disposed
  expect(f.rows.at(-1)).toMatchObject({responseId: 'response', sessionId: 'session', generation: 3,
    graphClosed: true, receivedAfterCancelPackets: 1, receivedAfterCancelSamples: 960,
    audit: {complete: true, drained: true, nonzeroSamplesAfterCancelUpper: 0, missingReason: null}})
  expect(f.node.disconnect).toHaveBeenCalledTimes(1)
  expect(f.node.port.close).toHaveBeenCalledTimes(1)
  await f.monitor.dispose(); expect(f.node.disconnect).toHaveBeenCalledTimes(1)
  expect(vi.getTimerCount()).toBe(0)
})

test('finish未確認はtimeoutで欠測とし、残るtimerとnodeを閉じる', async () => {
  const f = fixture(); f.output(); f.now(1010); f.monitor.cancel(1010)
  const disposed = f.monitor.dispose()
  await vi.advanceTimersByTimeAsync(2000); await disposed
  expect(f.rows.at(-1)).toMatchObject({graphClosed: true,
    audit: {complete: false, drained: false, missingReason: 'audit_closed_before_drain'}})
  expect(vi.getTimerCount()).toBe(0)
})

test('drain済みでも後着した非ゼロ出力の欠測通知で合格を取り消す', async () => {
  const f = fixture(); f.output(); f.now(1010); f.monitor.cancel(1010)
  await f.finish()
  expect(f.rows.at(-1)?.audit.complete).toBe(true)
  f.send({kind: 'missing', reason: 'audit_output_after_finish'})
  expect(f.rows.at(-1)?.audit).toMatchObject({complete: false, missingReason: 'audit_output_after_finish'})
  await f.monitor.dispose(); expect(vi.getTimerCount()).toBe(0)
})

test('cancel前の受信をstaleへ加算せず、通常graphのdrainも待つ', async () => {
  const f = fixture(); f.monitor.received(960); f.output()
  const disposed = f.monitor.dispose()
  await f.finish()
  await disposed
  expect(f.rows).toEqual([]); expect(f.node.disconnect).toHaveBeenCalledOnce(); expect(vi.getTimerCount()).toBe(0)
})


test.each([
  [{baseLatency: 128 / 48000, outputLatency: 0.008}, {baseLatencySeconds: 128 / 48000, outputLatencySeconds: 0.008}],
  [{baseLatency: 0, outputLatency: 0}, {baseLatencySeconds: 0, outputLatencySeconds: 0}],
  [{}, {baseLatencySeconds: null, outputLatencySeconds: null}],
  [{baseLatency: Number.NaN, outputLatency: Infinity}, {baseLatencySeconds: null, outputLatencySeconds: null}],
  [{baseLatency: -1, outputLatency: -1}, {baseLatencySeconds: null, outputLatencySeconds: null}],
])('ブラウザの実遅延を記録し、未対応・不正値を欠測とする (%j)', async (latencies, expected) => {
  const f = fixture(latencies); f.output(); f.now(1010); f.monitor.cancel(1010)
  expect(f.rows.at(-1)?.outputContext).toEqual({sampleRate: 48000, ...expected})
  await f.finish(); await f.monitor.dispose()
  expect(vi.getTimerCount()).toBe(0)
})


const stopOutput = (f: ReturnType<typeof fixture>, nonzero = 0) => {
  f.send({kind: 'output', confirmedFrame: 50048, intervals: [{startFrame: 50048, endFrame: 50176,
    nonzeroSamples: nonzero, firstNonzeroFrame: nonzero ? 50048 : null, lastNonzeroFrame: nonzero ? 50175 : null}]})
}
async function closeStopped(f: ReturnType<typeof fixture>) {
  const disposed = f.monitor.dispose()
  f.now(1061); f.clock({contextTime: 1.06, performanceTime: 1060})
  f.send({kind: 'finished', endFrame: 50176}); await disposed
  expect(vi.getTimerCount()).toBe(0)
}
test.each([true, false])('停止位置が実出力時計を通過するまでは確認を返さず、診断有無によらず同じ判定をする (%s)', async diagnostic => {
  const f = fixture({}, diagnostic); f.output(); f.now(1010)
  const pending = f.monitor.stopAndConfirm(), confirmed = vi.fn()
  void pending.then(confirmed)
  expect(f.monitor.stopAndConfirm()).toBe(pending)
  stopOutput(f); f.send({kind: 'stopped', endFrame: 50176})
  await Promise.resolve(); expect(confirmed).not.toHaveBeenCalled()
  expect(f.node.port.postMessage).toHaveBeenCalledTimes(1)
  expect(f.node.port.postMessage).toHaveBeenCalledWith({kind: 'stop'})
  f.now(1041); f.clock({contextTime: 1.05, performanceTime: 1050})
  await vi.advanceTimersByTimeAsync(5); expect(confirmed).not.toHaveBeenCalled()
  f.now(1051); await vi.advanceTimersByTimeAsync(5)
  expect(await pending).toMatchObject({endFrame: 50176, outputClockPassedFrame: 50176, observedAtMs: 1051})
  expect(f.node.disconnect).not.toHaveBeenCalled()
  expect(f.node.port.postMessage).not.toHaveBeenCalledWith({kind: 'finish'})
  await closeStopped(f)
})

test.each(['invalid_marker', 'old_marker', 'nonzero_marker', 'invalid_clock', 'timeout', 'closed'])('停止確認の%sを成功へ変換しない', async mode => {
  const f = fixture(); f.output(); f.now(1010)
  const pending = f.monitor.stopAndConfirm()
  const rejected = expect(pending).rejects.toThrow('output_stop_')
  stopOutput(f, mode === 'nonzero_marker' ? 128 : 0)
  if (mode === 'invalid_marker') f.send({kind: 'stopped', endFrame: -1})
  else if (mode === 'old_marker') f.send({kind: 'stopped', endFrame: 50048})
  else if (mode === 'nonzero_marker') f.send({kind: 'stopped', endFrame: 50176})
  else if (mode === 'invalid_clock') {
    f.send({kind: 'stopped', endFrame: 50176})
    f.now(1020); f.clock({contextTime: .9, performanceTime: 1019}); await vi.advanceTimersByTimeAsync(5)
  } else if (mode === 'timeout') await vi.advanceTimersByTimeAsync(1000)
  else {
    const disposed = f.monitor.dispose()
    f.now(1061); f.clock({contextTime: 1.06, performanceTime: 1060})
    f.send({kind: 'finished', endFrame: 50176}); await disposed
  }
  await rejected
  if (mode !== 'closed') await closeStopped(f)
  expect(f.rows.every(row => row.outputStopConfirmation === undefined)).toBe(true)
})

test('要求前の停止markerを後から有効な確認として採用しない', async () => {
  const f = fixture(); f.output(); f.send({kind: 'stopped', endFrame: 50048})
  await expect(f.monitor.stopAndConfirm()).rejects.toThrow('output_stop_marker_invalid')
  const disposed = f.monitor.dispose(); await f.finish(); await disposed
})
