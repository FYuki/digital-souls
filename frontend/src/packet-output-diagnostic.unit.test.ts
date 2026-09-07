import {expect, test} from 'vitest'
import {PacketOutputDiagnostic, type PacketOutputEvidence} from './livekit/packet-output-diagnostic'
import {analyzeFaultRecovery, type TimedProbe} from '../playwright/fault-recovery-diagnostic'
import type {DecodedAudioPacket} from './livekit/media-observer'
import type {PacketRenderInterval} from './livekit/packet-renderer'

const packet = (index = 0): DecodedAudioPacket => ({packetIndex: index, rtpTimestamp: 99 + index * 960,
  receivedAtMs: 110, decodedAtMs: 112, receivedAtBoundsMs: {lowerMs: 110, upperMs: 111},
  decodedAtBoundsMs: {lowerMs: 112, upperMs: 113}, source: 7, pcm: new Float32Array(960)})
const interval = (index = 0, offset = 0, count = 128): PacketRenderInterval => ({kind: 'rendered',
  packetIndex: index, rtpTimestamp: 99 + index * 960, packetSampleOffset: offset,
  startFrame: 48000 + offset, endFrame: 48000 + offset + count, energy: 1, firstAudibleFrame: 48000 + offset})
const control: TimedProbe = {status: 'received', generation: 0, probeId: 'nonce', sentAtMs: 110,
  receivedAtMs: 115, observedAtMs: 115}
const restored = {lowerMs: 100, upperMs: 105}
function capture() {
  const rows: PacketOutputEvidence[] = []
  const diagnostic = new PacketOutputDiagnostic('response', 'track', 0, row => rows.push(row))
  return {rows, diagnostic}
}

test('後続packetを含めて数値だけを記録し、出力未確認の受信だけでは回復にしない', () => {
  const {rows, diagnostic} = capture()
  diagnostic.receive(packet(1))
  expect(rows).toEqual([])
  diagnostic.confirm(interval(1), 120, 125)
  expect(rows).toHaveLength(1)
  expect(JSON.stringify(rows)).not.toContain('pcm')
  const result = analyzeFaultRecovery(restored, [control], rows, false, 0)
  expect(result.control_recovery_upper_ms).toBe(15)
  expect(result.audio_recovery_upper_ms).toBe(20)
  expect(result.recovery_upper_ms).toBe(20)
})

test('全出力後にmetadataを解放し、長時間出力の累積件数でoverflowしない', () => {
  const {rows, diagnostic} = capture()
  for (let i = 0; i < 300; i++) {
    diagnostic.receive(packet(i)); diagnostic.confirm(interval(i, 0, 960), 120, 145)
  }
  expect(rows.every(row => row.status === 'captured')).toBe(true)
})

test('未出力packetの滞留と未知packetは理由付き欠測にする', () => {
  const {rows, diagnostic} = capture()
  for (let i = 0; i < 257; i++) diagnostic.receive(packet(i))
  diagnostic.confirm(interval(999), 120, 125)
  expect(rows.map(row => row.status === 'missing' && row.reason))
    .toEqual(['packet_metadata_overflow', 'packet_metadata_missing'])
})

test('復旧前に受信済みのbufferと遅着ackを回復成功にしない', () => {
  const {rows, diagnostic} = capture()
  diagnostic.receive({...packet(), receivedAtMs: 99, receivedAtBoundsMs: {lowerMs: 99, upperMs: 100}})
  diagnostic.confirm(interval(), 120, 125)
  const result = analyzeFaultRecovery(restored, [{...control, sentAtMs: 104}], rows, false, 0)
  expect(result.control_recovery_upper_ms).toBeNull()
  expect(result.audio_recovery_upper_ms).toBeNull()
})

test('同一packetの隣接区間は許可し、重複提示区間を検出する', () => {
  const {rows, diagnostic} = capture()
  diagnostic.receive(packet())
  diagnostic.confirm(interval(), 120, 125)
  diagnostic.confirm(interval(0, 128), 123, 128)
  expect(analyzeFaultRecovery(restored, [control], rows, false, 0).duplicate_packet_output_intervals).toBe(0)
  diagnostic.confirm(interval(), 130, 135)
  expect(analyzeFaultRecovery(restored, [control], rows, false, 0).duplicate_packet_output_intervals).toBe(1)
})

test('無音・10秒超過・観測overflowを成功に補完しない', () => {
  const {rows, diagnostic} = capture()
  diagnostic.receive(packet())
  diagnostic.confirm({...interval(), energy: 0, firstAudibleFrame: undefined}, 120, 125)
  expect(analyzeFaultRecovery(restored, [control], rows, false, 0).audio_recovery_upper_ms).toBeNull()
  expect(analyzeFaultRecovery(restored, [{...control, sentAtMs: 10200, receivedAtMs: 10210}], [], true, 0))
    .toMatchObject({control_recovery_upper_ms: null, packet_evidence_missing: 1})
})


test('途中の再生経路エラーがあれば、重複観測0でも全出力の検証済みにはしない', () => {
  const {rows, diagnostic} = capture()
  diagnostic.receive(packet()); diagnostic.confirm(interval(), 120, 125)
  expect(analyzeFaultRecovery(restored, [control], rows, false, 1))
    .toMatchObject({duplicate_packet_output_intervals: 0, output_path_failures: 1, output_evidence_complete: false})
})
