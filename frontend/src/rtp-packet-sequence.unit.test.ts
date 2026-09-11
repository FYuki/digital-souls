import {expect, test} from 'vitest'
import {RtpPacketSequence, RtpPacketSequenceError} from './livekit/rtp-packet-sequence'

test('連続packetと32bit折り返しを受け付ける', () => {
  const sequence = new RtpPacketSequence()
  const first = 0xffffffff - 500
  expect(sequence.receive({packetIndex: 0, rtpTimestamp: first})).toBeUndefined()
  expect(sequence.receive({packetIndex: 1, rtpTimestamp: (first + 960) >>> 0})).toBeUndefined()
})

test('正の20ms単位の欠落を、欠落sampleの再生成功に補完しない', () => {
  const sequence = new RtpPacketSequence()
  sequence.receive({packetIndex: 0, rtpTimestamp: 99})
  expect(sequence.receive({packetIndex: 1, rtpTimestamp: 99 + 960 * 101}))
    .toEqual({expectedTimestamp: 1059, receivedTimestamp: 97059, missingPacketCount: 100})
})

test.each([99, 98, 100, NaN, 0x100000000])('重複・逆転・不正な時刻を通常のlossへ丸めない: %s', timestamp => {
  const sequence = new RtpPacketSequence()
  sequence.receive({packetIndex: 0, rtpTimestamp: 99})
  expect(() => sequence.receive({packetIndex: 1, rtpTimestamp: timestamp})).toThrow('RTP packet sequence invalid')
})

test('入力のpacket番号自体の欠落はプロトコル不整合とする', () => {
  const sequence = new RtpPacketSequence()
  expect(() => sequence.receive({packetIndex: 1, rtpTimestamp: 99})).toThrow()
})


test('不整合は前後の数値だけを残し、非有限値を診断へ流さない', () => {
  const sequence = new RtpPacketSequence()
  sequence.receive({packetIndex: 0, rtpTimestamp: 99, source: 7})
  try {sequence.receive({packetIndex: 1, rtpTimestamp: 100, source: 8}); throw new Error('expected failure')}
  catch (error) {
    expect(error).toBeInstanceOf(RtpPacketSequenceError)
    expect((error as RtpPacketSequenceError).context).toMatchObject({expectedPacketIndex: 1,
      packetIndex: 1, previousRtpTimestamp: 99, rtpTimestamp: 100, timestampDelta: 1, previousSource: 7, source: 8})
  }
  expect(new RtpPacketSequenceError({invalid: NaN}).context).toEqual({})
})
