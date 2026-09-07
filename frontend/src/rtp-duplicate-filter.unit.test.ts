import {expect, test} from 'vitest'
import {rtpDuplicateFilterSource} from './livekit/rtp-duplicate-filter'

type Filter = {accept: (packet: {source: number; rtpTimestamp: number; sequenceNumber?: number}, payload: Uint8Array) => string;
  seen: Map<string, Uint8Array>; bytes: number}
const makeFilter = () => new (new Function(rtpDuplicateFilterSource + '; return RtpDuplicateFilter')() as new () => Filter)()

test('同一送信元・時刻・primary payloadだけを復号前に重複として除外する', () => {
  const filter = makeFilter(), packet = {source: 7, rtpTimestamp: 99, sequenceNumber: 65535}
  expect(filter.accept(packet, new Uint8Array([1, 2, 3]))).toBe('accepted')
  expect(filter.accept(packet, new Uint8Array([1, 2, 3]))).toBe('duplicate')
  expect(filter.accept(packet, new Uint8Array([1, 2, 4]))).toBe('conflict')
  expect(filter.accept({...packet, source: 8}, new Uint8Array([1, 2, 3]))).toBe('accepted')
  expect(filter.accept({...packet, rtpTimestamp: 1059, sequenceNumber: 0}, new Uint8Array([1, 2, 3]))).toBe('accepted')
})

test('入力bufferの再利用で過去の同一性を壊さず、保持量も制限する', () => {
  const filter = makeFilter(), payload = new Uint8Array([1, 2, 3])
  filter.accept({source: 7, rtpTimestamp: 0, sequenceNumber: 0}, payload); payload[0] = 9
  expect(filter.accept({source: 7, rtpTimestamp: 0, sequenceNumber: 0}, new Uint8Array([1, 2, 3]))).toBe('duplicate')
  for (let i = 1; i <= 1100; i++) filter.accept({source: 7, rtpTimestamp: i, sequenceNumber: i}, new Uint8Array(1024))
  expect(filter.seen.size).toBeLessThanOrEqual(1024)
  expect(filter.bytes).toBeLessThanOrEqual(524288)
})


test('同一時刻の別連番はpayload一致でも再送とせず、時刻重複として区別する', () => {
  const filter = makeFilter(), packet = {source: 7, rtpTimestamp: 99, sequenceNumber: 1093}
  expect(filter.accept(packet, new Uint8Array([1, 2, 3]))).toBe('accepted')
  expect(filter.accept({...packet, sequenceNumber: 1094}, new Uint8Array([1, 2, 3]))).toBe('overlap')
  expect(filter.accept({...packet, sequenceNumber: 1094}, new Uint8Array(80))).toBe('overlap')
  expect(filter.accept({...packet, rtpTimestamp: 1059}, new Uint8Array([1, 2, 3]))).toBe('conflict')
  expect(filter.accept({source: 7, rtpTimestamp: 1059}, new Uint8Array([1, 2, 3]))).toBe('sequence_unavailable')
})
