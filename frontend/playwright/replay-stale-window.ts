import {replayPostGainOutput} from '../src/livekit/post-gain-archive'
import type {StaleAudioObservation} from '../src/livekit/post-gain-monitor'
import type {DecodedReceiptSnapshot} from '../src/livekit/decoded-receipt-audit'
import type {StaleTextRow} from './stale-text-probe'

type Bounds = {lowerMs: number; upperMs: number}
type Interval = Bounds & {units: number}
type Counts = {itemsLower: number; itemsUpper: number; unitsLower: number; unitsUpper: number}
const safeCount = (value: number) => Number.isSafeInteger(value) && value >= 0
const time = (value: number) => Number.isFinite(value) && value >= 0
const boundsValid = (b: Bounds) => time(b.lowerMs) && time(b.upperMs) && b.lowerMs <= b.upperMs
function count(rows: readonly Interval[], bounds: Bounds): Counts {
  const result = {itemsLower: 0, itemsUpper: 0, unitsLower: 0, unitsUpper: 0}
  for (const row of rows) {
    if (!boundsValid(row) || !Number.isSafeInteger(row.units) || row.units < 0) throw new Error('invalid_stale_interval')
    if (row.lowerMs >= bounds.upperMs) {result.itemsLower++; result.unitsLower += row.units}
    if (row.upperMs >= bounds.lowerMs) {result.itemsUpper++; result.unitsUpper += row.units}
  }
  if (!Number.isSafeInteger(result.unitsUpper)) throw new Error('stale_count_overflow')
  return result
}
export type StaleWindowInput = {
  output: StaleAudioObservation; receipts: DecodedReceiptSnapshot; text: StaleTextRow
  textClosed: boolean; textOverflow: boolean; bounds: Bounds
}
export function replayStaleWindow(input: StaleWindowInput) {
  const {output, receipts, text, bounds} = input
  if (!boundsValid(bounds) || !safeCount(output.generation) || output.generation !== receipts.generation || receipts.responseId !== output.responseId || text.responseId !== output.responseId
    || receipts.sessionId !== output.sessionId || text.sessionId !== output.sessionId) throw new Error('stale_identity_or_bounds_invalid')
  const audio = output.graphClosed ? replayPostGainOutput(output.outputArchive, bounds)
    : {complete: false, missingReason: 'output_graph_not_closed', audit: null}
  const receiptsMissing = receipts.boundary !== 'decoded_packet_callback' || receipts.overflow || receipts.missingReason !== null
    || receipts.closedAtMs === null || !time(receipts.closedAtMs) || !time(receipts.beganAtMs) || !time(receipts.retainedAfterMs)
    || receipts.closedAtMs < bounds.upperMs
    || receipts.beganAtMs > bounds.lowerMs || receipts.retainedAfterMs > bounds.lowerMs
  if (![receipts.totalPackets, receipts.totalSamples, receipts.discardedPackets, receipts.discardedSamples].every(safeCount)) {
    throw new Error('invalid_receipt_count')
  }
  if (!receiptsMissing && (receipts.entries.length + receipts.discardedPackets !== receipts.totalPackets
    || receipts.entries.reduce((sum, row) => sum + row.samples, 0) + receipts.discardedSamples !== receipts.totalSamples)) {
    throw new Error('receipt_count_mismatch')
  }
  let previousReceiptAtMs = -1
  const received = receiptsMissing ? {complete: false, missingReason: 'receipt_window_unobserved', counts: null}
    : {complete: true, missingReason: null, counts: count(receipts.entries.map(row => {
      if (!time(row.atMs) || row.atMs < receipts.beganAtMs || row.atMs < receipts.retainedAfterMs
        || row.atMs < previousReceiptAtMs || row.atMs > receipts.closedAtMs!) throw new Error('invalid_receipt_time')
      previousReceiptAtMs = row.atMs
      return {lowerMs: Math.max(0, row.atMs - 0.2), upperMs: row.atMs + 0.2, units: row.samples}
    }), bounds)}
  const textMissing = !input.textClosed || input.textOverflow || text.missingReason !== null || !text.domObserved
    || text.startedAtMs === null || text.cancelledAtMs === null
    || !time(text.observedFromMs) || !time(text.observedThroughMs)
    || !time(text.startedAtMs) || !time(text.cancelledAtMs)
    || text.observedFromMs > bounds.lowerMs || text.observedThroughMs < bounds.upperMs
  if (![text.receivedEvents, text.receivedCharacters, text.domChanges, text.domAddedCharacters].every(safeCount)) {
    throw new Error('invalid_text_count')
  }
  if (!textMissing) {
    for (const row of text.changes) {
      if (!['received', 'dom_added'].includes(row.kind) || !boundsValid(row)
        || row.lowerMs < Math.max(0, text.observedFromMs - 0.2)
        || row.upperMs > text.observedThroughMs + 0.2) throw new Error('invalid_text_change')
    }
    for (const kind of ['received', 'dom_added'] as const) {
      const rows = text.changes.filter(row => row.kind === kind)
      if (rows.length !== (kind === 'received' ? text.receivedEvents : text.domChanges)
        || rows.reduce((sum, row) => sum + row.characters, 0) !== (kind === 'received' ? text.receivedCharacters : text.domAddedCharacters)) {
        throw new Error('text_count_mismatch')
      }
    }
  }
  const textResult = textMissing ? {complete: false, missingReason: 'text_window_unobserved', received: null, presented: null}
    : {complete: true, missingReason: null,
      received: count(text.changes.filter(row => row.kind === 'received').map(row => ({...row, units: row.characters})), bounds),
      presented: count(text.changes.filter(row => row.kind === 'dom_added').map(row => ({...row, units: row.characters})), bounds)}
  return {bounds, audio, received, text: textResult,
    // 保存履歴・サーバー生成・全cohortの正式受け入れはこの個別窓だけでは証明しない。
    fullStaleAcceptanceVerified: false as const}
}
