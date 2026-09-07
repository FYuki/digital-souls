import {expect, test} from 'vitest'
import {rtpDuplicateFilterSource} from './livekit/rtp-duplicate-filter'

type Filter = {accept: (packet: {source: number; rtpTimestamp: number}, payload: Uint8Array) => string;
  seen: Map<string, Uint8Array>; bytes: number}
const makeFilter = () => new (new Function(rtpDuplicateFilterSource + '; return RtpDuplicateFilter')() as new () => Filter)()

test('同一送信元・時刻・primary payloadだけを復号前に重複として除外する', () => {
  const filter = makeFilter(), packet = {source: 7, rtpTimestamp: 99}
  expect(filter.accept(packet, new Uint8Array([1, 2, 3]))).toBe('accepted')
  expect(filter.accept(packet, new Uint8Array([1, 2, 3]))).toBe('duplicate')
  expect(filter.accept(packet, new Uint8Array([1, 2, 4]))).toBe('conflict')
  expect(filter.accept({...packet, source: 8}, new Uint8Array([1, 2, 3]))).toBe('accepted')
  expect(filter.accept({...packet, rtpTimestamp: 1059}, new Uint8Array([1, 2, 3]))).toBe('accepted')
})

test('入力bufferの再利用で過去の同一性を壊さず、保持量も制限する', () => {
  const filter = makeFilter(), payload = new Uint8Array([1, 2, 3])
  filter.accept({source: 7, rtpTimestamp: 0}, payload); payload[0] = 9
  expect(filter.accept({source: 7, rtpTimestamp: 0}, new Uint8Array([1, 2, 3]))).toBe('duplicate')
  for (let i = 1; i <= 1100; i++) filter.accept({source: 7, rtpTimestamp: i}, new Uint8Array(1024))
  expect(filter.seen.size).toBeLessThanOrEqual(1024)
  expect(filter.bytes).toBeLessThanOrEqual(524288)
})
