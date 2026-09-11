import {afterEach, expect, test, vi} from 'vitest'
import {opusPacketDecoderSource} from './livekit/opus-packet-decoder'
afterEach(() => vi.useRealTimers())
const setup = async () => {
  vi.useFakeTimers()
  let callback!: (frame: unknown) => void
  let error!: () => void
  const failure = vi.fn(), decode = vi.fn(), close = vi.fn()
  class Decoder {
    static isConfigSupported = async () => ({supported: true})
    state = 'configured'
    constructor(options: {output: typeof callback; error: typeof error}) { callback = options.output; error = options.error }
    configure = vi.fn()
    decode = decode
    close() { close(); this.state = 'closed' }
    flush() { throw new Error('flush must not be used between packets') }
  }
  const Kind = new Function('AudioDecoder', 'EncodedAudioChunk', opusPacketDecoderSource + '; return OpusPacketDecoder;')(
    Decoder, class {constructor(public data: unknown) {}},
  )
  const decoder = await Kind.create(failure)
  const output = (overrides = {}) => {
    const frame = {numberOfFrames: 960, sampleRate: 48000, numberOfChannels: 1, timestamp: 999,
      copyTo: (target: Float32Array) => target.fill(.2), close: vi.fn(), ...overrides}
    callback(frame)
    return frame
  }
  return {Kind, decoder, output, failure, decode, close, error: () => error()}
}
test('連続packetは復号状態を維持し、再構成timestampではなく入力番号へ対応する', async () => {
  const p = await setup()
  for (let index = 0; index < 3; index++) {
    const pending = p.decoder.decode(new Uint8Array([0x98, 0]), index)
    const frame = p.output()
    expect(await pending).toMatchObject({packetIndex: index, timestamp: 999, samples: expect.any(Float32Array)})
    expect(frame.close).toHaveBeenCalledOnce()
  }
  expect(p.decode).toHaveBeenCalledTimes(3)
  expect(p.failure).not.toHaveBeenCalled()
  p.decoder.close()
  expect(vi.getTimerCount()).toBe(0)
  expect(p.close).toHaveBeenCalledOnce()
})
test('未完了入力を別packetで上書きしない', async () => {
  const p = await setup()
  const first = p.decoder.decode(new Uint8Array([0x98]), 0)
  await expect(p.decoder.decode(new Uint8Array([0x98]), 1)).rejects.toThrow('already_pending')
  p.output()
  expect((await first).packetIndex).toBe(0)
  p.decoder.close()
})
test.each(['timeout', 'error', 'short', 'stereo', 'nonfinite', 'copy_error', 'close'])('異常出力や終了を相関済みPCMへ変換しない（%s）', async mode => {
  const p = await setup()
  const rejected = expect(p.decoder.decode(new Uint8Array([0x98]), 0)).rejects.toThrow()
  let frame
  if (mode === 'timeout') await vi.advanceTimersByTimeAsync(1000)
  else if (mode === 'error') p.error()
  else if (mode === 'close') p.decoder.close()
  else frame = p.output(mode === 'short' ? {numberOfFrames: 480} : mode === 'stereo' ? {numberOfChannels: 2}
    : mode === 'nonfinite' ? {copyTo: (target: Float32Array) => target.fill(NaN)}
      : {copyTo: () => {throw new Error('copy failed')}})
  await rejected
  if (frame) expect(frame.close).toHaveBeenCalledOnce()
  expect(p.close).toHaveBeenCalledOnce()
  expect(vi.getTimerCount()).toBe(0)
  await expect(p.decoder.decode(new Uint8Array([0x98]), 1)).rejects.toThrow('closed')
})
test('RED headerと冗長payloadを除き20ms primary Opusだけを取り出す', async () => {
  const p = await setup()
  const red = new Uint8Array([0xef, 0, 0, 2, 0x6f, 5, 6, 0x98, 7, 8])
  expect(p.Kind.primaryPayload(red.buffer, 'audio/red')).toEqual(new Uint8Array([0x98, 7, 8]))
  p.decoder.close()
})
test.each([
  [[], 'audio/opus'], [[0x80], 'audio/opus'], [[0x9b], 'audio/opus'],
  [[0x9b, 0], 'audio/opus'], [[0xef], 'audio/red'], [[0xef, 0, 3, 255, 0x6f, 0x98], 'audio/red'],
  [[0x6f], 'audio/red'], [[0x98], 'audio/PCMU'],
])('破損RED・未対応codec・20ms以外のpacketを拒否する（%j）', async (bytes, mime) => {
  const p = await setup()
  expect(() => p.Kind.primaryPayload(new Uint8Array(bytes as number[]).buffer, mime)).toThrow()
  p.decoder.close()
})
