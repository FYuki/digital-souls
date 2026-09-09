import {expect, test} from 'vitest'
import {PostGainOutputArchive, replayPostGainOutput, type OutputArchiveEntry} from './livekit/post-gain-archive'
import {replayConfirmedOutputStop, type OutputStopProof} from './livekit/output-stop-proof'
import type {StaleAudioObservation} from './livekit/post-gain-monitor'

const id = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`
function fixture() {
  const proof: OutputStopProof = {sessionId: id(1), responseId: id(2), requestId: id(3), graphId: id(4), generation: 1,
    serverOrderNs: ['90071992547410000', '90071992547410001', '90071992547410002', '90071992547410003', '90071992547410004']}
  const archive = new PostGainOutputArchive()
  const output = (atMs: number, start: number, n: number, nonzero: number | null = null) => archive.record({atMs,
    kind: 'message', message: {kind: 'output', confirmedFrame: start + (n - 1) * 128,
      intervals: Array.from({length: n}, (_, i) => ({startFrame: start + i * 128, endFrame: start + (i + 1) * 128,
        nonzeroSamples: i === nonzero ? 128 : 0, firstNonzeroFrame: i === nonzero ? start + i * 128 : null,
        lastNonzeroFrame: i === nonzero ? start + (i + 1) * 128 - 1 : null}))}})
  const clock = (atMs: number, ms: number) => archive.record({atMs, kind: 'clock',
    timestamp: {contextTime: ms / 1000, performanceTime: ms}, sampleRate: 48000})
  output(1005, 48000, 4)
  clock(1005.5, 1005)
  output(1011, 48512, 3, 2)
  archive.record({kind: 'stop_requested', atMs: 1012, sessionId: proof.sessionId, responseId: proof.responseId,
    requestId: proof.requestId, graphId: proof.graphId, generation: proof.generation})
  output(1013, 48896, 1)
  archive.record({kind: 'stop_marker', atMs: 1013, endFrame: 49024})
  clock(1023.5, 1023)
  archive.record({kind: 'stop_confirmed', atMs: 1023.5, requestId: proof.requestId, endFrame: 49024, outputClockPassedFrame: 49024})
  output(1024, 49024, 16)
  archive.lock(1040)
  clock(1041.5, 1041)
  archive.record({kind: 'message', atMs: 1045, message: {kind: 'finished', endFrame: 51072}})
  clock(1065.5, 1065)
  archive.close(1066)
  const observation = {sessionId: proof.sessionId, responseId: proof.responseId, generation: 1,
    outputGraphId: proof.graphId, graphClosed: true, outputArchive: archive.snapshot(),
    outputStopConfirmation: {endFrame: 49024, outputClockPassedFrame: 49024, observedAtMs: 1023.5}} as StaleAudioObservation
  return {observation, proof, bounds: {lowerMs: 1010, upperMs: 1035}}
}
test('時計probeが停止前まで含む場合だけ、独立に確認した出力frameで粗い下限を補う', () => {
  const f = fixture()
  expect(replayPostGainOutput(f.observation.outputArchive, f.bounds)).toMatchObject({complete: true,
    audit: {nonzeroSamplesAfterCancelLower: 0, nonzeroSamplesAfterCancelUpper: 128,
      clockCorrelationMethod: 'bracketing_output_timestamps'}})
  expect(replayConfirmedOutputStop(f.observation, f.bounds, f.proof)).toMatchObject({complete: true, stopProofVerified: true,
    audit: {nonzeroSamplesAfterCancelLower: 0, nonzeroSamplesAfterCancelUpper: 0,
      cancelBoundsMs: f.bounds, cancelOutputFrameBounds: {lowerFrame: 49024},
      clockCorrelationMethod: 'bracketing_output_timestamps_with_confirmed_stop'}})
})

test.each(['no_request', 'no_marker', 'no_confirmation', 'duplicate_request', 'duplicate_marker', 'duplicate_confirmation',
  'request_after_marker', 'wrong_request', 'wrong_session', 'wrong_response', 'wrong_generation', 'wrong_graph',
  'stale_marker', 'nonzero_stop_interval', 'nonzero_after_marker', 'inflated_output_clock', 'future_output_clock',
  'snapshot_clock_changed', 'snapshot_time_changed', 'graph_not_closed', 'archive_not_closed', 'source_missing',
  'confirmation_after_cancel', 'server_integer_precision', 'server_time_float', 'server_time_missing', 'null_proof',
  'multiple_graphs', 'unknown_archive_entry'] as const)('停止証拠の反例をゼロに補完しない: %s', mutation => {
  const f = fixture()
  let proof: OutputStopProof | null = f.proof
  let observation = f.observation
  const entries = structuredClone(observation.outputArchive.entries) as Array<OutputArchiveEntry>
  const request = entries.findIndex(e => e.kind === 'stop_requested')
  const marker = entries.findIndex(e => e.kind === 'stop_marker')
  const confirmed = entries.findIndex(e => e.kind === 'stop_confirmed')
  if (mutation === 'no_request') entries.splice(request, 1)
  if (mutation === 'no_marker') entries.splice(marker, 1)
  if (mutation === 'no_confirmation') entries.splice(confirmed, 1)
  for (const [name, position] of [['duplicate_request', request], ['duplicate_marker', marker], ['duplicate_confirmation', confirmed]] as const) {
    if (mutation === name) entries.splice(position, 0, entries[position])
  }
  if (mutation === 'request_after_marker') {
    entries.splice(request, 1)
    entries.splice(marker, 0, {...f.observation.outputArchive.entries[request], atMs: 1013})
  }
  if (mutation === 'wrong_request') proof = {...proof, requestId: id(99)}
  if (mutation === 'wrong_session') proof = {...proof, sessionId: id(99)}
  if (mutation === 'wrong_response') proof = {...proof, responseId: id(99)}
  if (mutation === 'wrong_generation') proof = {...proof, generation: 2}
  if (mutation === 'wrong_graph') proof = {...proof, graphId: id(99)}
  if (mutation === 'multiple_graphs') entries.splice(request, 0, {...entries[request], graphId: id(99)} as OutputArchiveEntry)
  if (mutation === 'stale_marker') entries[marker] = {...entries[marker], endFrame: 48896} as OutputArchiveEntry
  if (mutation === 'nonzero_stop_interval' || mutation === 'nonzero_after_marker') {
    const position = mutation === 'nonzero_stop_interval' ? marker - 1 : confirmed + 1
    const entry = entries[position]
    if (entry.kind !== 'message' || entry.message.kind !== 'output') throw new Error('invalid fixture')
    const row = entry.message.intervals[0]
    entries[position] = {...entry, message: {...entry.message, intervals: [
      {...row, nonzeroSamples: 1, firstNonzeroFrame: row.startFrame, lastNonzeroFrame: row.startFrame},
      ...entry.message.intervals.slice(1)]}}
  }
  if (mutation === 'inflated_output_clock') entries[confirmed] = {...entries[confirmed], outputClockPassedFrame: 99999} as OutputArchiveEntry
  if (mutation === 'future_output_clock') entries[confirmed - 1] = {...entries[confirmed - 1], timestamp: {contextTime: 2, performanceTime: 2000}} as OutputArchiveEntry
  if (mutation === 'snapshot_clock_changed') observation = {...observation, outputStopConfirmation: {...observation.outputStopConfirmation!, outputClockPassedFrame: 99999}}
  if (mutation === 'snapshot_time_changed') observation = {...observation, outputStopConfirmation: {...observation.outputStopConfirmation!, observedAtMs: 1024}}
  if (mutation === 'graph_not_closed') observation = {...observation, graphClosed: false}
  if (mutation === 'archive_not_closed') observation = {...observation, outputArchive: {...observation.outputArchive, closedAtMs: null}}
  if (mutation === 'source_missing') observation = {...observation, outputArchive: {...observation.outputArchive, sourceMissing: 'audit_output_gap'}}
  if (mutation === 'confirmation_after_cancel' || mutation === 'server_integer_precision') proof = {...proof,
    // Numberなら同じ値に丸められる1nsの逆転も拒否する。
    serverOrderNs: ['90071992547410000', '90071992547410001', '90071992547410004', '90071992547410003', '90071992547410005']}
  if (mutation === 'server_time_float') proof = {...proof, serverOrderNs: ['1', '2', '3.0', '4', '5']}
  if (mutation === 'server_time_missing') proof = {...proof, serverOrderNs: ['1', '2', '3', '4']}
  if (mutation === 'null_proof') proof = null
  if (mutation === 'unknown_archive_entry') entries.splice(request, 0, {kind: 'unknown', atMs: 1012} as unknown as OutputArchiveEntry)
  observation = {...observation, outputArchive: {...observation.outputArchive, entries}}
  expect(replayConfirmedOutputStop(observation, f.bounds, proof)).toMatchObject({complete: false, stopProofVerified: false})
})
