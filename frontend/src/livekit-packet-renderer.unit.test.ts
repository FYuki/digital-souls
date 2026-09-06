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
  return {messages, send: (data: unknown) => instance.port.onmessage?.({data}),
    step: (frame: number) => {setFrame(frame); const output = new Float32Array(128); instance.process([], [[output]]); return output}}
}
const packet = (index: number, value = .25) => ({kind: 'pcm', packetIndex: index,
  rtpTimestamp: 99 + index * 960, samples: new Float32Array(960).fill(value)})

test('入力前と初回buffer中のゼロを応答sampleとして記録しない', () => {
  const p = renderer()
  expect(p.step(0)).toEqual(new Float32Array(128))
  p.send(packet(0))
  for (let frame = 0; frame < 2880; frame += 128) {
    const result = p.step(frame)
    if (frame + 128 <= 2880) expect(result).toEqual(new Float32Array(128))
  }
  expect(p.messages[0]).toMatchObject({kind: 'rendered', packetIndex: 0, packetSampleOffset: 0, startFrame: 2880})
})

test('全PCMをpacket内offsetと出力frameへ対応づけ、本文は観測へ含めない', () => {
  const p = renderer()
  p.send(packet(0, 0))
  p.send(packet(1, .25))
  const output: number[] = []
  for (let frame = 2880; frame < 4800; frame += 128) output.push(...p.step(frame))
  expect(output).toEqual([...new Float32Array(960), ...new Float32Array(960).fill(.25)])
  const rendered = p.messages.filter(row => row.kind === 'rendered')
  expect(rendered.reduce((total, row) => total + Number(row.endFrame) - Number(row.startFrame), 0)).toBe(1920)
  expect(rendered[0]).toMatchObject({packetIndex: 0, packetSampleOffset: 0, startFrame: 2880, energy: 0})
  expect(rendered.at(-1)).toMatchObject({packetIndex: 1, endFrame: 4800})
  expect(rendered.every(row => !('samples' in row))).toBe(true)
})

test('stopは未出力PCMを破棄し、遅着した旧packetやresumeでも再開しない', () => {
  const p = renderer()
  p.send(packet(0)); p.send(packet(1))
  p.step(2880)
  const count = p.messages.length
  p.send({kind: 'stop'}); p.send(packet(2)); p.send({kind: 'resume'})
  expect(p.step(3008)).toEqual(new Float32Array(128))
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
