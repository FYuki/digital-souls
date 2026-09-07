import {expect, test, vi} from 'vitest'
import {attachIdleVadReset} from './idle-vad-reset'
const frame = (ms: number, amplitude = 0) => new Float32Array(ms * 16).fill(amplitude)
const harness = () => {
  const events: string[] = []
  const process = vi.fn(async (_samples: Float32Array) => {events.push('process')})
  const vad = {processFrame: process, pause: vi.fn(async () => {events.push('pause')}),
    start: vi.fn(async () => {events.push('start')})}
  const onError = vi.fn()
  const control = attachIdleVadReset(vad, onError)
  return {vad, process, events, control, onError}
}

test('700ms未満の静音や文中休止ではresetせず、受信frameを順に保持する', async () => {
  const h = harness()
  const input = frame(96, 0.1)
  const first = h.vad.processFrame(input)
  input.fill(0)
  await first
  await h.vad.processFrame(frame(600))
  await h.vad.processFrame(frame(96, 0.1))
  expect(h.vad.pause).not.toHaveBeenCalled()
  expect(h.process.mock.calls[0][0][0]).toBeCloseTo(0.1)
  expect(h.process).toHaveBeenCalledTimes(3)
})

test('静音中だけ公開pause/startで状態をresetしてから同じframeを処理する', async () => {
  const h = harness()
  for (let i = 0; i < 8; i++) await h.vad.processFrame(frame(96))
  expect(h.events.slice(-3)).toEqual(['pause', 'start', 'process'])
  for (let i = 0; i < 2; i++) await h.vad.processFrame(frame(96))
  expect(h.vad.pause).toHaveBeenCalledTimes(1)
  await h.vad.processFrame(frame(96))
  expect(h.vad.pause).toHaveBeenCalledTimes(2)
  expect(h.process).toHaveBeenCalledTimes(11)
})

test('停止要求後はpause待ちから再開せず、待機frameも処理しない', async () => {
  const h = harness()
  let release!: () => void
  h.vad.pause.mockImplementationOnce(() => new Promise<void>(resolve => {release = resolve}))
  const first = h.vad.processFrame(frame(700))
  await vi.waitFor(() => expect(h.vad.pause).toHaveBeenCalledTimes(1))
  const queued = h.vad.processFrame(frame(96, 0.1))
  const closing = h.control.close()
  release()
  await Promise.all([first, queued, closing])
  expect(h.vad.start).not.toHaveBeenCalled()
  expect(h.process).not.toHaveBeenCalled()
})

test('モデル処理が失敗した後は追加frameを処理しない', async () => {
  const h = harness(), error = new Error('model failure')
  h.process.mockRejectedValueOnce(error)
  await h.vad.processFrame(frame(96, 0.1))
  await h.vad.processFrame(frame(96, 0.1))
  expect(h.onError).toHaveBeenCalledWith(error)
  expect(h.process).toHaveBeenCalledTimes(1)
  await h.control.close()
})


test('処理が詰まってもframeを無制限に保持せず、欠落を黙って続行しない', async () => {
  const h = harness()
  const pending = Array.from({length: 17}, () => h.vad.processFrame(frame(96, 0.1)))
  await Promise.all(pending)
  expect(h.onError).toHaveBeenCalledTimes(1)
  expect(h.onError.mock.calls[0][0].message).toContain('backlog')
  expect(h.process).not.toHaveBeenCalled()
})
