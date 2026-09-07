import {afterEach, beforeEach, expect, test, vi} from 'vitest'
import {PostGainAudioMonitor, type StaleAudioObservation} from './livekit/post-gain-monitor'
import type {GainAuditMessage} from './livekit/post-gain-audit'

class FakeNode {
  port = {onmessage: null as ((event: MessageEvent<GainAuditMessage>) => void) | null,
    postMessage: vi.fn(), close: vi.fn()}
  connect = vi.fn()
  disconnect = vi.fn()
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.stubGlobal('AudioWorkletNode', FakeNode)
})
afterEach(() => {vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals()})
function fixture() {
  let now = 1000
  let clock: AudioTimestamp = {contextTime: 0, performanceTime: 0}
  vi.spyOn(performance, 'now').mockImplementation(() => now)
  const context = {sampleRate: 48000, destination: {}, getOutputTimestamp: () => clock} as AudioContext
  const rows: StaleAudioObservation[] = []
  const monitor = new PostGainAudioMonitor(context, 'response', 'session', 3, row => rows.push(row))
  const node = monitor.node as unknown as FakeNode
  const send = (data: GainAuditMessage) => node.port.onmessage?.({data} as MessageEvent<GainAuditMessage>)
  const output = () => send({kind: 'output', confirmedFrame: 48128, intervals: [
    {startFrame: 48000, endFrame: 48128, nonzeroSamples: 128, firstNonzeroFrame: 48000, lastNonzeroFrame: 48127},
    {startFrame: 48128, endFrame: 48256, nonzeroSamples: 0, firstNonzeroFrame: null, lastNonzeroFrame: null},
  ]})
  return {monitor, node, rows, send, output, now: (n: number) => {now = n},
    clock: (value: AudioTimestamp) => {clock = value}}
}

test('cancel後もnodeと出力時計の観測を保持し、drain後のdisposeで閉じる', async () => {
  const f = fixture(); f.output(); f.now(1003.1); f.monitor.cancel(1003.1)
  expect(f.node.disconnect).not.toHaveBeenCalled()
  expect(f.node.port.postMessage).toHaveBeenCalledWith({kind: 'finish'})
  expect(f.rows.at(-1)?.audit.complete).toBe(false)
  f.monitor.received(960)
  const disposed = f.monitor.dispose()
  expect(f.node.port.close).not.toHaveBeenCalled()
  f.now(1100); f.clock({contextTime: 1.01, performanceTime: 1010}); f.send({kind: 'finished', endFrame: 48256})
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
  const f = fixture(); f.now(1003); f.monitor.cancel(1003)
  const disposed = f.monitor.dispose()
  await vi.advanceTimersByTimeAsync(2000); await disposed
  expect(f.rows.at(-1)).toMatchObject({graphClosed: true,
    audit: {complete: false, drained: false, missingReason: 'audit_closed_before_drain'}})
  expect(vi.getTimerCount()).toBe(0)
})

test('drain済みでも後着した非ゼロ出力の欠測通知で合格を取り消す', async () => {
  const f = fixture(); f.output(); f.now(1003); f.monitor.cancel(1003)
  f.now(1100); f.clock({contextTime: 1.01, performanceTime: 1010}); f.send({kind: 'finished', endFrame: 48256})
  expect(f.rows.at(-1)?.audit.complete).toBe(true)
  f.send({kind: 'missing', reason: 'audit_output_after_finish'})
  expect(f.rows.at(-1)?.audit).toMatchObject({complete: false, missingReason: 'audit_output_after_finish'})
  await f.monitor.dispose(); expect(vi.getTimerCount()).toBe(0)
})

test('cancel前の受信をstaleへ加算せず、通常graphのdrainも待つ', async () => {
  const f = fixture(); f.monitor.received(960); f.output()
  const disposed = f.monitor.dispose()
  f.now(1100); f.clock({contextTime: 1.01, performanceTime: 1010}); f.send({kind: 'finished', endFrame: 48256})
  await disposed
  expect(f.rows).toEqual([]); expect(f.node.disconnect).toHaveBeenCalledOnce(); expect(vi.getTimerCount()).toBe(0)
})
