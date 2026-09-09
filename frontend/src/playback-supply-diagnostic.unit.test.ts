import {afterEach, expect, test, vi} from 'vitest'
import {installPlaybackSupplyDiagnostic, readPlaybackSupplyDiagnostic} from '../playwright/playback-supply-diagnostic'
import type {PacketOutputEvidence} from './livekit/packet-output-diagnostic'

function setup() {
  const port = {bindRoom: vi.fn()}
  vi.stubGlobal('window', {__digitalSoulsVoiceSessionTestPort: port})
  installPlaybackSupplyDiagnostic()
  let observe!: (row: PacketOutputEvidence) => void
  port.bindRoom({setPacketOutputObserver: (callback: typeof observe) => {observe = callback}})
  return observe
}
afterEach(() => vi.unstubAllGlobals())
function row(index: number, start: number, end: number, offset = 0): PacketOutputEvidence {
  const received = index === 0 ? 100 : 240
  return {status: 'captured', responseId: 'response-private', trackSid: 'track-private', generation: 0,
    packet: {packetIndex: index, rtpTimestamp: 99 + index * 960, source: 7,
      receivedAtMs: received, decodedAtMs: received + 2,
      receivedAtBoundsMs: {lowerMs: received, upperMs: received + .2},
      decodedAtBoundsMs: {lowerMs: received + 2, upperMs: received + 2.2},
      mainReceivedAtMs: received + 10},
    interval: {kind: 'rendered', packetIndex: index, rtpTimestamp: 99 + index * 960,
      packetSampleOffset: offset, startFrame: start, endFrame: end, energy: 1},
    outputAtMs: 200 + start / 48, confirmedAtMs: 200 + end / 48 + 1}
}

test('全packetの遅延と音切れ前後を数値化し、分割intervalでpacketを重複計上しない', () => {
  const observe = setup()
  observe(row(0, 0, 128)); observe(row(0, 128, 960, 128))
  observe(row(1, 2944, 3904))
  const result = readPlaybackSupplyDiagnostic('response-private')
  expect(result.overflow).toBe(false)
  expect(result.summary).toMatchObject({packet_count: 2, rendered_samples: 1920,
    gap_count: 1, gap_samples: 1984, maximum_gap_samples: 1984, missing_observations: 0,
    maximum_receive_interval_upper_ms: 140.2, maximum_main_delivery_upper_ms: 8})
  expect(result.summary?.gaps[0]).toMatchObject({gap_samples: 1984, packet_index: 1,
    previous_packet_index: 0, packet_sample_offset: 0, main_delivery_upper_ms: 8})
  expect(JSON.stringify(result)).not.toMatch(/response-private|track-private|pcm/)
})

test('配送時刻の欠測や区間の重複を診断成功へ補完しない', () => {
  const observe = setup()
  const value = row(0, 0, 960)
  if (value.status !== 'captured') throw new Error('fixture')
  observe({...value, packet: {...value.packet, mainReceivedAtMs: undefined}})
  observe(value); observe(value)
  expect(readPlaybackSupplyDiagnostic('response-private').summary).toMatchObject({packet_count: 1,
    rendered_samples: 960, missing_observations: 2})
  expect(readPlaybackSupplyDiagnostic('unknown').summary).toBeNull()
})

test('応答数の上限超過を記録し、無制限にpacketや本文を保存しない', () => {
  const observe = setup()
  for (let i = 0; i < 5; i++) observe({...row(0, 0, 960), responseId: `response-${i}`})
  expect(readPlaybackSupplyDiagnostic('response-4')).toEqual({summary: null, overflow: true})
})
