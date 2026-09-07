// 実Sileroの保存済み確率と元PCMを用いる候補policy診断。Browser実時間の受け入れとは区別する。
import { readFile, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { transform } from 'esbuild'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(resolve(root, 'package.json'))
const { Resampler } = require('@ricky0123/vad-web/dist/resampler')
const manifest = JSON.parse(await readFile(resolve(root, 'playwright/fixtures/voice-quality-v2/manifest.json'), 'utf8'))
const probabilities = JSON.parse(await readFile(resolve(process.argv[2]), 'utf8'))
const source = await readFile(process.argv[4] ? resolve(process.argv[4]) : resolve(root, 'src/lib/audio/utterance-detector.ts'), 'utf8')
const { code } = await transform(source, { loader: 'ts', format: 'esm' })
const { UtteranceDetector, utteranceDetectorOptions } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
const hash = bytes => createHash('sha256').update(bytes).digest('hex')
const byHash = new Map(probabilities.trials.map(trial => [trial.fixture_sha256, trial]))
const trials = []
for (const fixture of manifest.trials) {
  const bytes = await readFile(resolve(root, 'test-results/vad-quality/fixtures-v2', `${fixture.id}.wav`))
  if (hash(bytes) !== fixture.audio_sha256) throw new Error('fixture hash mismatch')
  // materializerのwave writerが生成するPCM16 RIFF。必ずdata chunkを探索する。
  let pcm
  for (let offset = 12; offset + 8 <= bytes.length;) {
    const size = bytes.readUInt32LE(offset + 4)
    if (offset + 8 + size > bytes.length) throw new Error('invalid WAV')
    if (bytes.toString('ascii', offset, offset + 4) === 'data') pcm = bytes.subarray(offset + 8, offset + 8 + size)
    offset += 8 + size + size % 2
  }
  if (!pcm) throw new Error('missing PCM')
  const audio = Float32Array.from({length: pcm.length / 2}, (_, i) => pcm.readInt16LE(i * 2) / 32768)
  const events = []
  const detector = new UtteranceDetector(event => events.push(event))
  const row = byHash.get(fixture.audio_sha256)
  if (!row) throw new Error('missing real model evidence')
  const resampler = new Resampler({nativeSampleRate: 48000, targetSampleRate: 16000, targetFrameSize: probabilities.frame_ms * 16})
  let index = 0
  for await (const frame of resampler.stream(audio)) {
    detector.process(frame, row.frame_probabilities[index], (++index) * probabilities.frame_ms)
  }
  if (index !== row.frame_probabilities.length) throw new Error('frame count mismatch')
  const ended = events.filter(event => event.type === 'ended')
  const firstConfirmed = events.find(event => event.type === 'confirmed')
  const start = fixture.speech_intervals[0].start_sample / 48
  const end = fixture.speech_intervals.at(-1).end_sample / 48
  trials.push({fixture_sha256: fixture.audio_sha256, cohort: fixture.cohort, events,
    missed: ended.length === 0,
    leading_loss: !firstConfirmed || firstConfirmed.speechStartedAtMs - start > 100,
    early_end: ended.some(event => end - event.detectedAtMs > 100),
    split: ended.length > 1,
    finalize_delay_ms: ended.length === 1 ? ended[0].detectedAtMs - end : null})
}
const p95 = values => {
  if (!values.length) return null
  values.sort((a,b)=>a-b); const index=(values.length-1)*.95
  return values[Math.floor(index)]+(values[Math.ceil(index)]-values[Math.floor(index)])*(index%1)
}
const summary = Object.fromEntries(['backchannel','take_turn','pause'].map(cohort => {
  const rows=trials.filter(row=>row.cohort===cohort)
  return [cohort,{denominator:rows.length, ...Object.fromEntries(['missed','leading_loss','early_end','split'].map(key=>[key,rows.filter(row=>row[key]).length])),
    finalize_delay_valid:rows.filter(row=>row.finalize_delay_ms!==null).length,
    finalize_delay_p95_ms:p95(rows.flatMap(row=>row.finalize_delay_ms===null?[]:[row.finalize_delay_ms]))}]
}))
const output = resolve(process.argv[3])
await writeFile(output,JSON.stringify({scope:'pcm_and_recorded_silero_policy_diagnostic',options:utteranceDetectorOptions,
  detector_sha256:hash(source), model_sha256:probabilities.model_sha256, frame_ms:probabilities.frame_ms,summary,trials},null,2)+'\n',{flag:'wx'})
console.log(JSON.stringify({summary,output}))
