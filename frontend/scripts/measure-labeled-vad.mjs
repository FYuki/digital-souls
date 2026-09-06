import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { transform } from 'esbuild'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(resolve(root, 'package.json'))
const { FrameProcessor, Message, getDefaultRealTimeVADOptions } = require('@ricky0123/vad-web')
const { SileroLegacy, SileroV5 } = require('@ricky0123/vad-web/dist/models')
const { Resampler } = require('@ricky0123/vad-web/dist/resampler')
const ort = require('onnxruntime-web/wasm')
const fixtureRoot = resolve(process.argv[4] ?? resolve(root, 'playwright/fixtures/voice-quality-v1'))
const materializedRoot = resolve(process.argv[5] ?? resolve(root, 'test-results/vad-quality/fixtures'))
const output = resolve(process.argv[2] ?? resolve(root, 'test-results/vad-quality/labeled-v1.json'))
const manifestBytes = await readFile(resolve(fixtureRoot, 'manifest.json'))
const manifest = JSON.parse(manifestBytes)
const hash = bytes => createHash('sha256').update(bytes).digest('hex')
const policySource = await readFile(resolve(root, 'src/lib/audio/vad-policy.ts'), 'utf8')
const policyModule = await transform(policySource, { loader: 'ts', format: 'esm' })
const policy = await import(`data:text/javascript;base64,${Buffer.from(policyModule.code).toString('base64')}`)
const experimentalOverrides = process.argv[6] ? JSON.parse(await readFile(resolve(process.argv[6]), 'utf8')) : {}
const optionNames = ['positiveSpeechThreshold', 'negativeSpeechThreshold', 'preSpeechPadMs', 'redemptionMs', 'minSpeechMs']
if (Object.entries(experimentalOverrides).some(([key, value]) => !optionNames.includes(key)
  || typeof value !== 'number' || !Number.isFinite(value) || value <= 0)) throw new Error('invalid experimental VAD options')
const options = { ...getDefaultRealTimeVADOptions('legacy'), redemptionMs: policy.VAD_UTTERANCE_REDEMPTION_MS, ...experimentalOverrides }
if (options.positiveSpeechThreshold > 1 || options.negativeSpeechThreshold > options.positiveSpeechThreshold) throw new Error('invalid VAD hysteresis thresholds')
const modelName = process.argv[3] ?? 'legacy'
if (!['legacy', 'v5'].includes(modelName)) throw new Error('model must be legacy or v5')
const frameMs = modelName === 'v5' ? 32 : 96
const modelPath = resolve(root, `node_modules/@ricky0123/vad-web/dist/silero_vad_${modelName}.onnx`)
const modelBytes = await readFile(modelPath)
ort.env.wasm.numThreads = 1
const model = await (modelName === 'v5' ? SileroV5 : SileroLegacy).new(ort, async () => modelBytes.buffer.slice(modelBytes.byteOffset, modelBytes.byteOffset + modelBytes.byteLength))
const processor = new FrameProcessor(model.process, model.reset_state, options, frameMs)
processor.resume()

function pcmFromWav(bytes) {
  let format, pcm
  for (let offset = 12; offset + 8 <= bytes.length;) {
    const size = bytes.readUInt32LE(offset + 4)
    if (offset + 8 + size > bytes.length) throw new Error('invalid fixture WAV')
    const chunk = bytes.toString('ascii', offset, offset + 4)
    if (chunk === 'fmt ') format = bytes.subarray(offset + 8, offset + 8 + size)
    if (chunk === 'data') pcm = bytes.subarray(offset + 8, offset + 8 + size)
    offset += 8 + size + size % 2
  }
  if (!format || !pcm || format.readUInt16LE(0) !== 1 || format.readUInt16LE(2) !== 1
    || format.readUInt32LE(4) !== 48000 || format.readUInt16LE(14) !== 16) throw new Error('fixture must be 48 kHz mono PCM16')
  return Float32Array.from({ length: pcm.length / 2 }, (_, index) => pcm.readInt16LE(index * 2) / 32768)
}

const trials = []
try {
  for (const trial of manifest.trials) {
    const data = await readFile(resolve(materializedRoot, `${trial.id}.wav`))
    if (hash(data) !== trial.audio_sha256) throw new Error('materialized fixture hash mismatch')
    processor.reset()
    const resampler = new Resampler({ nativeSampleRate: 48000, targetSampleRate: 16000, targetFrameSize: frameMs * 16 })
    let frames = 0, misfires = 0, detectedAt = null, confirmedAt = null
    const segments = []
    const frameProbabilities = []
    for await (const frame of resampler.stream(pcmFromWav(data))) {
      const nowMs = (++frames) * frameMs
      await processor.process(frame, event => {
        if (event.msg === Message.FrameProcessed) frameProbabilities.push(event.probs.isSpeech)
        if (event.msg === Message.SpeechStart) detectedAt = nowMs
        if (event.msg === Message.SpeechRealStart) confirmedAt = nowMs
        if (event.msg === Message.VADMisfire) { misfires++; detectedAt = null; confirmedAt = null }
        if (event.msg === Message.SpeechEnd) {
          segments.push({ captured_start_ms: nowMs - event.audio.length / 16, captured_end_ms: nowMs,
            speech_detected_ms: detectedAt, speech_confirmed_ms: confirmedAt, finalized_ms: nowMs })
          detectedAt = null; confirmedAt = null
        }
      })
    }
    const start = trial.speech_intervals[0].start_sample / 48
    const end = trial.speech_intervals.at(-1).end_sample / 48
    trials.push({ fixture_sha256: trial.audio_sha256, cohort: trial.cohort,
      speech_start_ms: start, speech_end_ms: end, pause_ms: trial.pause_samples / 48,
      segments, frame_probabilities: frameProbabilities, misfires, unterminated: processor.speaking,
      missed_utterance: segments.length === 0,
      leading_loss_over_100_ms: segments.length === 0 || segments[0].captured_start_ms - start > 100,
      early_end_over_100_ms: segments.length > 0 && segments.some(segment => end - segment.captured_end_ms > 100),
      unexpected_split: segments.length > 1,
      finalize_delay_ms: segments.length === 1 ? segments[0].finalized_ms - end : null })
  }
} finally { await model.release() }

const percentile = (values, fraction) => {
  if (values.length === 0) return null
  values.sort((a, b) => a - b)
  const index = (values.length - 1) * fraction
  return values[Math.floor(index)] + (values[Math.ceil(index)] - values[Math.floor(index)]) * (index % 1)
}
const summary = Object.fromEntries(['backchannel', 'take_turn', 'pause'].map(cohort => {
  const rows = trials.filter(trial => trial.cohort === cohort)
  return [cohort, { denominator: rows.length,
    ...Object.fromEntries(['missed_utterance', 'leading_loss_over_100_ms', 'early_end_over_100_ms', 'unexpected_split', 'unterminated']
      .map(key => [key, rows.filter(row => row[key]).length])),
    finalize_delay_valid: rows.filter(row => row.finalize_delay_ms !== null).length,
    finalize_delay_missing: rows.filter(row => row.finalize_delay_ms === null).length,
    finalize_delay_p95_ms: percentile(rows.flatMap(row => row.finalize_delay_ms === null ? [] : [row.finalize_delay_ms]), 0.95) }]
}))
await mkdir(dirname(output), { recursive: true })
await writeFile(output, JSON.stringify({ measurement_scope: 'labeled_silero_diagnostic',
  manifest_sha256: hash(manifestBytes), model_sha256: hash(modelBytes), policy_sha256: hash(policySource),
  model: modelName, production_model: modelName === 'legacy', experimental_options: Object.keys(experimentalOverrides).length > 0, frame_ms: frameMs, options, summary, trials }, null, 2) + '\n')
process.stdout.write(JSON.stringify({ summary, output }) + '\n')
