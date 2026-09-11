import { readFile, mkdir, writeFile } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(resolve(root, 'package.json'))
const { FrameProcessor, Message, getDefaultRealTimeVADOptions } = require('@ricky0123/vad-web')
const { SileroLegacy, SileroV5 } = require('@ricky0123/vad-web/dist/models')
const { Resampler } = require('@ricky0123/vad-web/dist/resampler')
const ort = require('onnxruntime-web/wasm')
const modelName = process.argv[3] ?? 'legacy'
if (!['legacy', 'v5'].includes(modelName)) throw new Error('model must be legacy or v5')
const frameSamples = modelName === 'v5' ? 512 : 1536
const frameMs = frameSamples / 16
const redemptionMs = Number(process.argv[2] ?? '700')
if (!Number.isInteger(redemptionMs) || redemptionMs < 100 || redemptionMs > 2000) {
  throw new Error('redemption milliseconds must be between 100 and 2000')
}
const output = resolve(root, 'test-results', 'vad-quality', `${modelName}-redemption-${redemptionMs}.json`)
const fixture = JSON.parse(await readFile(resolve(root, 'playwright/fixtures/speech.metadata.json'), 'utf8'))
const wav = await readFile(resolve(root, 'playwright/fixtures/speech.wav'))
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex')
if (hash(wav) !== fixture.audio_sha256) throw new Error('fixture hash mismatch')
let pcm
let format
for (let offset = 12; offset + 8 <= wav.length;) {
  const size = wav.readUInt32LE(offset + 4)
  if (offset + 8 + size > wav.length) throw new Error('invalid WAV chunk')
  const name = wav.toString('ascii', offset, offset + 4)
  if (name === 'fmt ') format = wav.subarray(offset + 8, offset + 8 + size)
  if (name === 'data') pcm = wav.subarray(offset + 8, offset + 8 + size)
  offset += 8 + size + size % 2
}
if (!pcm || !format || format.readUInt16LE(0) !== 1 || format.readUInt16LE(2) !== 1
  || format.readUInt32LE(4) !== 48000 || format.readUInt16LE(14) !== 16) {
  throw new Error('fixture must be 48 kHz mono PCM16')
}
const source = Float32Array.from({ length: pcm.length / 2 }, (_,i) => pcm.readInt16LE(i * 2) / 32768)
const speech = source.slice(fixture.speech_start_sample, fixture.speech_end_sample)
const modelPath = resolve(root, `node_modules/@ricky0123/vad-web/dist/silero_vad_${modelName}.onnx`)
ort.env.wasm.numThreads = 1
const model = await (modelName === 'v5' ? SileroV5 : SileroLegacy).new(ort, async () => {
  const bytes = await readFile(modelPath)
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)
})
const options = { ...getDefaultRealTimeVADOptions(modelName), redemptionMs }
const processor = new FrameProcessor(model.process, model.reset_state, options, frameMs)
processor.resume()
async function measure(audio) {
  processor.reset()
  const resampler = new Resampler({ nativeSampleRate: 48000, targetSampleRate: 16000, targetFrameSize: frameSamples })
  const observed = []
  let frameIndex = 0
  let detectedAt = null
  for await (const frame of resampler.stream(audio)) {
    const decisionMs = (++frameIndex) * frameMs
    await processor.process(frame, (event) => {
      if (event.msg === Message.SpeechStart) detectedAt = decisionMs
      if (event.msg === Message.SpeechEnd) observed.push({
        decision_ms: decisionMs,
        captured_start_ms: decisionMs - event.audio.length / 16,
        captured_end_ms: decisionMs,
        speech_start_detection_ms: detectedAt,
      })
    })
  }
  // fixture末尾で強制確定しない。VAD自身が終端しない試行は未確定として残す。
  return { observed, unterminated: processor.speaking }
}
const trials = []
// 位相・無音長・音量を固定列で変え、各試行でSileroのstateもリセットする。
// 同一単語由来の合成fixtureであり、語彙の網羅性やbrowser実時間の代用にはしない。
for (let index = 0; index < 100; index += 1) {
  const leadingSamples = 4800 + (index % 96) * 48
  const pauseMs = [200, 400, 600][index % 3]
  const pauseSamples = pauseMs * 48
  const gain = [0.7, 1, 1.2, 0.85][index % 4]
  const speechEnd = leadingSamples + 2 * speech.length + pauseSamples
  const audio = new Float32Array(speechEnd + 48000 * 2)
  const scaled = speech.map((sample) => Math.max(-1, Math.min(1, sample * gain)))
  audio.set(scaled, leadingSamples)
  audio.set(scaled, leadingSamples + speech.length + pauseSamples)
  const { observed, unterminated } = await measure(audio)
  const startMs = leadingSamples / 48
  const endMs = speechEnd / 48
  trials.push({
    fixture_sha256: hash(new Uint8Array(audio.buffer)),
    expected_utterances: 1, pause_ms: pauseMs, gain,
    speech_start_ms: startMs, speech_end_ms: endMs, observed, unterminated,
    leading_loss_over_100_ms: observed.length === 0 || observed[0].captured_start_ms - startMs > 100,
    early_end_over_100_ms: observed.length === 0 || endMs - observed.at(-1).captured_end_ms > 100,
    split_at_intentional_pause: observed.length > 1,
    missed_utterance: observed.length === 0,
  })
}
const count = (key) => trials.filter((trial) => trial[key]).length
const summary = {
  trials: trials.length,
  leading_loss_over_100_ms: count('leading_loss_over_100_ms'),
  early_end_over_100_ms: count('early_end_over_100_ms'),
  split_at_intentional_pause: count('split_at_intentional_pause'),
  missed_utterance: count('missed_utterance'),
  unterminated: count('unterminated'),
}
await mkdir(dirname(output), { recursive: true })
await writeFile(output, JSON.stringify({
  measurement_scope: 'silero_fixture_diagnostic',
  model: `silero_vad_${modelName}`, frame_ms: frameMs, model_sha256: hash(await readFile(modelPath)),
  source_audio_sha256: fixture.audio_sha256,
  redemption_ms: redemptionMs, sample_rate_hz: 48000,
  summary, trials,
}, null, 2))
await model.release()
const delays = trials.filter((trial) => trial.observed.length === 1).map((trial) => trial.observed[0].decision_ms - trial.speech_end_ms).sort((a,b) => a-b)
const p95Index = (delays.length - 1) * 0.95
const p95 = delays.length === 0 ? null : delays[Math.floor(p95Index)] + (delays[Math.ceil(p95Index)] - delays[Math.floor(p95Index)]) * (p95Index % 1)
process.stdout.write(JSON.stringify({ model: modelName, redemption_ms: redemptionMs, ...summary, finalize_delay_p95_ms: p95, output }) + '\n')
