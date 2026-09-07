import {expect, test} from 'vitest'
import {PostGainOutputArchive, replayPostGainOutput} from './livekit/post-gain-archive'
import {DecodedReceiptAudit, type DecodedReceiptSnapshot} from './livekit/decoded-receipt-audit'

const fixture = () => {
  const archive = new PostGainOutputArchive()
  archive.record({atMs: 1005, kind: 'message', message: {kind: 'output', confirmedFrame: 49920,
    intervals: Array.from({length: 16}, (_, i) => ({startFrame: 48000 + i * 128, endFrame: 48128 + i * 128,
      nonzeroSamples: i === 6 ? 128 : 0, firstNonzeroFrame: i === 6 ? 48768 : null, lastNonzeroFrame: i === 6 ? 48895 : null}))}})
  for (const ms of [1009, 1012, 1029, 1032]) archive.record({atMs: ms + 0.5, kind: 'clock',
    timestamp: {contextTime: ms / 1000, performanceTime: ms}, sampleRate: 48000})
  archive.lock(1040)
  archive.record({atMs: 1045, kind: 'message', message: {kind: 'finished', endFrame: 50048}})
  archive.record({atMs: 1051, kind: 'clock', timestamp: {contextTime: 1.05, performanceTime: 1050}, sampleRate: 48000})
  archive.close(1052)
  return archive
}

test('clientcancelより前に出力した音声を、server境界のreplayで拾う', () => {
  const archive = fixture().snapshot()
  expect(replayPostGainOutput(archive, {lowerMs: 1030, upperMs: 1030})).toMatchObject({complete: true,
    audit: {nonzeroSamplesAfterCancelLower: 0, nonzeroSamplesAfterCancelUpper: 0}})
  expect(replayPostGainOutput(archive, {lowerMs: 1010, upperMs: 1011})).toMatchObject({complete: true,
    audit: {nonzeroSamplesAfterCancelLower: 128, nonzeroSamplesAfterCancelUpper: 128}})
})
test('未閉鎖・欠測・切り詰め・順序の改変をstale0へ補完しない', () => {
  const snapshot = fixture().snapshot(), bounds = {lowerMs: 1010, upperMs: 1011}
  for (const changed of [{closedAtMs: null}, {sourceMissing: 'audit_output_clock_invalid' as const},
    {overflow: true}, {clockInvalid: true}, {retainedAfterMs: 1010.1}, {entries: [...snapshot.entries].reverse()}]) {
    expect(replayPostGainOutput({...snapshot, ...changed}, bounds)).toMatchObject({complete: false, audit: null})
  }
  expect(replayPostGainOutput(snapshot, {lowerMs: 999, upperMs: 1011})).toMatchObject({complete: false,
    audit: {missingReason: 'audit_cancel_anchor_missing'}})
})
test('履歴を4秒へ制限し、cancel後に照合対象を切り捨てず、overflowを明示する', () => {
  const a = new PostGainOutputArchive()
  for (const atMs of [1000, 3000, 6000]) a.record({atMs, kind: 'clock', timestamp: {contextTime: 1, performanceTime: 100}, sampleRate: 48000})
  expect(a.snapshot().entries.map(r => r.atMs)).toEqual([3000, 6000])
  a.lock(6001)
  a.record({atMs: 11000, kind: 'clock', timestamp: {}, sampleRate: 48000})
  expect(a.snapshot().retainedAfterMs).toBe(2000)
  expect(a.snapshot().entries[0].atMs).toBe(3000)
  for (let i = 0; i < 4096; i++) a.record({atMs: 11000, kind: 'clock', timestamp: {}, sampleRate: 48000})
  expect(a.snapshot().overflow).toBe(true); expect(a.snapshot().entries).toHaveLength(4096)
})
test('snapshotを書き換えて内部の過去観測を変更できない', () => {
  const a = fixture(), first = a.snapshot()
  const clock = first.entries.find(row => row.kind === 'clock')!
  if (clock.kind === 'clock') clock.timestamp.contextTime = 999
  expect(a.snapshot()).not.toEqual(first)
})
test('graph前後の受信をcallbackの寿命で保持し、終了後の呼出しは欠測へする', () => {
  const reports: DecodedReceiptSnapshot[] = []
  const a = new DecodedReceiptAudit('response', 'session', 1, 1000, row => reports.push(row))
  a.received(960, 1010); a.received(480, 1020)
  expect(reports).toEqual([])
  a.cancel(1025); a.received(128, 1030); a.close(1040)
  expect(reports.at(-1)).toMatchObject({closedAtMs: 1040, missingReason: null,
    entries: [{atMs: 1010, samples: 960}, {atMs: 1020, samples: 480}, {atMs: 1030, samples: 128}]})
  a.received(128, 1050)
  expect(reports.at(-1)?.missingReason).toBe('receipt_after_close')
})
test('受信sample不正、時計逆行、古い区間の不足を記録する', () => {
  const a = new DecodedReceiptAudit('r', 's', 0, 1000, () => undefined)
  a.received(1, 1100); a.received(2, 6000)
  expect(a.snapshot()).toMatchObject({retainedAfterMs: 2000, entries: [{atMs: 6000, samples: 2}]})
  a.cancel(6001); a.received(1.5, 6002)
  expect(a.snapshot().missingReason).toBe('receipt_sample_invalid')
  const b = new DecodedReceiptAudit('r', 's', 0, 1000, () => undefined)
  b.received(1, 999); expect(b.snapshot().missingReason).toBe('receipt_clock_invalid')
})
