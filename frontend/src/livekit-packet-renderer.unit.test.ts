import {expect, test} from 'vitest'
import {packetRendererSource, PacketOutputTracker, type PacketRenderInterval} from './livekit/packet-renderer'

const renderer = () => {
  const messages: Array<Record<string, unknown>> = []
  class Base {
    port = {onmessage: null as ((event: {data: unknown}) => void) | null, postMessage: (message: Record<string, unknown>) => messages.push(message)}
  }
  let instance!: Base & {process: (_input: unknown[], outputs: Float32Array[][]) => boolean}
  const setFrame = new Function('AudioWorkletProcessor', 'registerProcessor',
    'let currentFrame = 0;' + packetRendererSource + '; return value => {currentFrame = value}')(
    Base, (_name: string, Kind: new (options: unknown) => typeof instance) => {instance = new Kind({processorOptions: {}})},
  )
  setFrame(0); instance.process([], [[new Float32Array(128)]]);
  setFrame(128); instance.process([], [[new Float32Array(128)]]);
  setFrame(256);
  return {messages, send: (data: unknown) => instance.port.onmessage?.({data}),
    step: (frame: number) => {setFrame(frame + 256); const output = new Float32Array(128); instance.process([], [[output]]); return output}}
}
const packet = (index: number, value = .25) => ({kind: 'pcm', packetIndex: index,
  rtpTimestamp: 99 + index * 960, samples: new Float32Array(960).fill(value)})

test('入力前と初回buffer中のゼロを応答sampleとして記録しない', () => {
  const p = renderer()
  expect(p.step(0)).toEqual(new Float32Array(128))
  p.send(packet(0))
  for (let frame = 128; frame <= 1536; frame += 128) {
    const result = p.step(frame)
    if (frame + 128 <= 1536) expect(result).toEqual(new Float32Array(128))
  }
  expect(p.messages[0]).toMatchObject({kind: 'rendered', packetIndex: 0, packetSampleOffset: 0, startFrame: 1792})
})

test('全PCMをpacket内offsetと出力frameへ対応づけ、本文は観測へ含めない', () => {
  const p = renderer()
  p.send(packet(0, 0))
  p.send(packet(1, .25))
  const output: number[] = []
  for (let frame = 1536; frame < 3456; frame += 128) output.push(...p.step(frame))
  expect(output).toEqual([...new Float32Array(960), ...new Float32Array(960).fill(.25)])
  const rendered = p.messages.filter(row => row.kind === 'rendered')
  expect(rendered.reduce((total, row) => total + Number(row.endFrame) - Number(row.startFrame), 0)).toBe(1920)
  expect(rendered[0]).toMatchObject({packetIndex: 0, packetSampleOffset: 0, startFrame: 1792, energy: 0})
  expect(rendered.at(-1)).toMatchObject({packetIndex: 1, endFrame: 3712})
  expect(rendered.every(row => !('samples' in row))).toBe(true)
})

test('stopは未出力PCMを破棄し、遅着した旧packetやresumeでも再開しない', () => {
  const p = renderer()
  p.send(packet(0)); p.send(packet(1))
  p.step(1536)
  const count = p.messages.length
  p.send({kind: 'stop'}); p.send(packet(2)); p.send({kind: 'resume'})
  expect(p.step(1664)).toEqual(new Float32Array(128))
  expect(p.messages).toHaveLength(count)
})

test('PCM待機量に上限を設け、重複packetを追加しない', () => {
  const p = renderer()
  p.send(packet(0)); p.send(packet(0))
  expect(p.messages.at(-1)).toMatchObject({kind: 'error', reason: 'packet_or_sample_mismatch'})
  for (let index = 1; index < 51; index++) p.send(packet(index))
  expect(p.messages.at(-1)).toMatchObject({kind: 'error', reason: 'pcm_queue_overflow'})
})

const interval: PacketRenderInterval = {kind: 'rendered', packetIndex: 0, rtpTimestamp: 99,
  packetSampleOffset: 0, startFrame: 48000, endFrame: 48128, energy: 0}

test('出力時計が区間末尾を通過する前は再生完了を通知しない', () => {
  const confirmed: Array<[PacketRenderInterval, number]> = []
  const tracker = new PacketOutputTracker((row, time) => confirmed.push([row, time]))
  tracker.record(interval)
  tracker.poll({contextTime: 1, performanceTime: 1000}, 48000)
  expect(confirmed).toEqual([])
  tracker.poll({contextTime: 2, performanceTime: 2000}, 48000)
  expect(confirmed).toEqual([[interval, 1000]])
  tracker.poll({contextTime: 3, performanceTime: 3000}, 48000)
  expect(confirmed).toHaveLength(1)
})

test.each(['duplicate', 'offset', 'packet', 'overlap', 'nonfinite'])('誤った出力相関を拒否する（%s）', damage => {
  const tracker = new PacketOutputTracker(() => undefined)
  tracker.record(interval)
  const next = {...interval, startFrame: 48128, endFrame: 48256, packetSampleOffset: 128}
  if (damage === 'duplicate') Object.assign(next, interval)
  if (damage === 'offset') next.packetSampleOffset = 129
  if (damage === 'packet') next.packetIndex = 1
  if (damage === 'overlap') next.startFrame = 48000
  if (damage === 'nonfinite') next.energy = NaN
  expect(() => tracker.record(next)).toThrow()
})

test('停止時点で出力時計が未通過の区間を後から完了扱いにしない', () => {
  const confirmed: unknown[] = []
  const tracker = new PacketOutputTracker(row => confirmed.push(row))
  tracker.record(interval); tracker.stop()
  tracker.poll({contextTime: 2, performanceTime: 2000}, 48000)
  expect(confirmed).toEqual([])
})


test.each([false, true])('完了通知が前後どちらに届いても全sampleの出力時計通過を待つ（先行=%s）', early => {
  const complete: unknown[] = []
  const tracker = new PacketOutputTracker(() => undefined, row => complete.push(row))
  const source = {inputSampleCount: 960, capturedSampleCount: 1920, paddingSampleCount: 960}
  if (early) tracker.finish(source)
  tracker.record({...interval, endFrame: 48960})
  tracker.poll({contextTime: 1.03, performanceTime: 1030}, 48000)
  expect(complete).toEqual([])
  tracker.record({...interval, packetIndex: 1, rtpTimestamp: 1059, startFrame: 49200, endFrame: 50160})
  tracker.poll({contextTime: 1.04, performanceTime: 1040}, 48000)
  expect(complete).toEqual([])
  tracker.poll({contextTime: 1.1, performanceTime: 1100}, 48000)
  if (!early) tracker.finish(source)
  expect(complete).toEqual([expect.objectContaining({expectedSamples: 1920, renderedSamples: 1920,
    inputSamples: 960, paddingSamples: 960, packetCount: 2, gapSamples: 240, maximumGapSamples: 240, gapCount: 1})])
  tracker.finish(source)
  tracker.poll({contextTime: 100, performanceTime: 100000}, 48000)
  expect(complete).toHaveLength(1)
})

test('末尾packetが欠けたまま時間が経っても再生完了やgapゼロへ補完しない', () => {
  const complete: unknown[] = []
  const tracker = new PacketOutputTracker(() => undefined, row => complete.push(row))
  tracker.finish({inputSampleCount: 960, capturedSampleCount: 1920, paddingSampleCount: 960})
  tracker.record({...interval, endFrame: 48960})
  tracker.poll({contextTime: 100, performanceTime: 100000}, 48000)
  expect(complete).toEqual([])
})

test('RTP欠落・重複を受信packet番号の連番だけで成功扱いしない', () => {
  const tracker = new PacketOutputTracker(() => undefined)
  tracker.record({...interval, endFrame: 48960})
  expect(() => tracker.record({...interval, packetIndex: 1, rtpTimestamp: 2019, startFrame: 48960, endFrame: 49920})).toThrow('RTP timeline')
})

test('矛盾する総sample数や終了後の追加sampleを拒否する', () => {
  const tracker = new PacketOutputTracker(() => undefined)
  expect(() => tracker.finish({inputSampleCount: 1, capturedSampleCount: 960, paddingSampleCount: 1})).toThrow()
  tracker.finish({inputSampleCount: 0, capturedSampleCount: 960, paddingSampleCount: 960})
  expect(() => tracker.finish({inputSampleCount: 960, capturedSampleCount: 1920, paddingSampleCount: 960})).toThrow()
  tracker.record({...interval, endFrame: 48960})
  expect(() => tracker.record({...interval, packetIndex: 1, rtpTimestamp: 1059, startFrame: 48960, endFrame: 49920})).toThrow()
})


test('公開frameの更新欠落でもPCMを重複・欠落させず、次の時計で確認する', () => {
  const p = renderer()
  p.send({...packet(0), samples: Float32Array.from({length: 960}, (_, i) => i / 1000)})
  const first = p.step(1536)
  const second = p.step(1536)
  expect(p.messages).toHaveLength(1)
  expect(second).not.toEqual(first)
  const third = p.step(1792)
  expect([...first, ...second, ...third]).toEqual([...Float32Array.from({length: 384}, (_, i) => i / 1000)])
  expect(p.messages.map(row => [row.startFrame, row.endFrame, row.renderClockConfirmationFrame]))
    .toEqual([[1792, 1920, 1792], [1920, 2048, 2048], [2048, 2176, 2048]])
})
test('次の公開時計が計数と違う場合は保留した出力を確認済みへ昇格しない', () => {
  const p = renderer()
  p.send(packet(0)); p.step(1536); p.step(1536); p.step(1664)
  expect(p.messages.filter(row => row.kind === 'rendered')).toHaveLength(1)
  expect(p.messages.at(-1)).toMatchObject({kind: 'error', reason: 'render_clock_unreconciled'})
})
test('停止した応答の未照合区間は後から届いた時計で提示済みにしない', () => {
  const p = renderer()
  p.send(packet(0)); p.step(1536); p.step(1536); p.send({kind: 'stop'})
  expect(p.step(1792)).toEqual(new Float32Array(128))
  expect(p.messages).toHaveLength(1)
})
