import {expect, test} from 'vitest'
import {replayStaleWindow, type StaleWindowInput} from '../playwright/replay-stale-window'
import {PostGainOutputArchive} from './livekit/post-gain-archive'

function fixture(): StaleWindowInput {
  const archive = new PostGainOutputArchive(); archive.close(1040)
  return {bounds: {lowerMs: 1015, upperMs: 1025}, textClosed: true, textOverflow: false,
    output: {sessionId: 's', responseId: 'r', generation: 0, graphClosed: true, outputArchive: archive.snapshot()} as StaleWindowInput['output'],
    receipts: {sessionId: 's', responseId: 'r', generation: 0, boundary: 'decoded_packet_callback', beganAtMs: 1000,
      retainedAfterMs: 0, cancelledAtMs: 1035, closedAtMs: 1040, overflow: false, missingReason: null,
      totalPackets: 3, totalSamples: 1568, discardedPackets: 0, discardedSamples: 0,
      entries: [{atMs: 1010, samples: 960}, {atMs: 1020, samples: 128}, {atMs: 1030, samples: 480}]},
    text: {sessionId: 's', responseId: 'r', startedAtMs: 1000, cancelledAtMs: 1035,
      observedFromMs: 1000, observedThroughMs: 1040, receivedEvents: 2, receivedCharacters: 5,
      duplicateEvents: 0, receivedAfterCancelEvents: 0, receivedAfterCancelCharacters: 0,
      domObserved: true, domChanges: 2, domAddedCharacters: 5, domAfterCancelChanges: 0,
      domAfterCancelAddedCharacters: 0, missingReason: null, changes: [
        {kind: 'received', lowerMs: 1009.8, upperMs: 1010.2, characters: 2},
        {kind: 'dom_added', lowerMs: 1011, upperMs: 1020, characters: 2},
        {kind: 'received', lowerMs: 1020.8, upperMs: 1021.2, characters: 3},
        {kind: 'dom_added', lowerMs: 1030, upperMs: 1032, characters: 3},
      ]}}
}
test('servercancel区間を跨ぐ受信・DOM追加を上限に残し、確実な追加は下限にも数える', () => {
  const result = replayStaleWindow(fixture())
  expect(result.received).toMatchObject({complete: true, counts: {itemsLower: 1, itemsUpper: 2, unitsLower: 480, unitsUpper: 608}})
  expect(result.text).toMatchObject({complete: true, received: {unitsLower: 0, unitsUpper: 3},
    presented: {itemsLower: 1, itemsUpper: 2, unitsLower: 3, unitsUpper: 5}})
  expect(result.audio.complete).toBe(false)
  expect(result.fullStaleAcceptanceVerified).toBe(false)
})
test('件数不一致・別応答・観測外の時刻を拒否する', () => {
  const a = fixture(); a.text.changes.pop(); expect(() => replayStaleWindow(a)).toThrow('text_count_mismatch')
  const b = fixture(); b.receipts = {...b.receipts, entries: b.receipts.entries.slice(1)}
  expect(() => replayStaleWindow(b)).toThrow('receipt_count_mismatch')
  const c = fixture(); c.text.responseId = 'other'; expect(() => replayStaleWindow(c)).toThrow('stale_identity_or_bounds_invalid')
  const d = fixture(); d.text.changes[0] = {...d.text.changes[0], upperMs: 1050}
  expect(() => replayStaleWindow(d)).toThrow('invalid_text_change')
})
test('overflow・閉鎖前・履歴不足を成功にしない', () => {
  const a = fixture(); a.textOverflow = true; a.receipts = {...a.receipts, overflow: true}
  expect(replayStaleWindow(a)).toMatchObject({text: {complete: false}, received: {complete: false}})
  const b = fixture(); b.text.observedFromMs = 1020; b.receipts = {...b.receipts, retainedAfterMs: 1020}
  expect(replayStaleWindow(b)).toMatchObject({text: {complete: false}, received: {complete: false}})
  const c = fixture(); c.textClosed = false; c.receipts = {...c.receipts, closedAtMs: null}
  expect(replayStaleWindow(c)).toMatchObject({text: {complete: false}, received: {complete: false}})
})

test('負数・NaN・件数相殺・逆順callback・別世代を受け入れない', () => {
  for (const value of [-1, Number.NaN, 0.5, Number.MAX_SAFE_INTEGER + 1]) {
    const a = fixture(); a.receipts = {...a.receipts, totalPackets: value, discardedPackets: value - 3}
    expect(() => replayStaleWindow(a)).toThrow('invalid_receipt_count')
    const b = fixture(); b.text.receivedCharacters = value
    expect(() => replayStaleWindow(b)).toThrow('invalid_text_count')
  }
  const c = fixture(); c.receipts = {...c.receipts, entries: [...c.receipts.entries].reverse()}
  expect(() => replayStaleWindow(c)).toThrow('invalid_receipt_time')
  const d = fixture(); d.receipts = {...d.receipts, generation: 1}
  expect(() => replayStaleWindow(d)).toThrow('stale_identity_or_bounds_invalid')
})
test('archiveの閉鎖時刻やcutoffのNaNを境界通過の証拠にしない', () => {
  for (const field of ['closedAtMs', 'retainedAfterMs', 'lockedAtMs']) {
    const a = fixture()
    a.output = {...a.output, outputArchive: {...a.output.outputArchive, [field]: Number.NaN}}
    expect(replayStaleWindow(a).audio).toMatchObject({complete: false, missingReason: 'output_archive_invalid'})
  }
})
