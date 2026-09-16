// #358: 既存FE実装を、同じ正規化済みPCMに対するBE移設の比較元として実行する。
import {readFile} from 'node:fs/promises'
import {createRequire} from 'node:module'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(root + '/package.json')
const {transform} = require('esbuild')
const {SileroLegacy} = require('@ricky0123/vad-web/dist/models')
const ort = require('onnxruntime-web/wasm')
ort.env.wasm.numThreads = 1
const load = async path => {
  const {code} = await transform(await readFile(path, 'utf8'), {loader: 'ts', format: 'esm'})
  return import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))
}
const {UtteranceDetector} = await load(root + '/src/lib/audio/utterance-detector.ts')
const {createShortSpeechAnalyzer} = await load(root + '/src/lib/audio/short-speech-evidence.ts')
const {attachIdleVadReset} = await load(root + '/src/lib/audio/idle-vad-reset.ts')
const modelBytes = await readFile(root + '/node_modules/@ricky0123/vad-web/dist/silero_vad_legacy.onnx')
const wasm = await readFile(root + '/src/lib/audio/vendor/libfvad.wasm')
const pcm = await readFile(process.argv[2])
if (!pcm.length || pcm.length % (1536 * 2) !== 0 || pcm.length > 16000 * 2 * 120) throw Error('invalid parity fixture')
const model = await SileroLegacy.new(ort, async () => modelBytes.buffer.slice(modelBytes.byteOffset, modelBytes.byteOffset + modelBytes.byteLength))
const analyzer = await createShortSpeechAnalyzer(wasm.buffer.slice(wasm.byteOffset, wasm.byteOffset + wasm.byteLength))
const events = [], frames = [], resets = []
const detector = new UtteranceDetector(event => events.push({
  kind: event.type, started_sample: Math.round(event.speechStartedAtMs * 16), detected_sample: Math.round(event.detectedAtMs * 16),
}))
let end = 0
const vad = {
  pause: async () => model.reset_state(), start: async () => {},
  processFrame: async frame => {
    const {isSpeech} = await model.process(frame)
    const evidence = analyzer.process(frame)
    detector.process(frame, isSpeech, end / 16, evidence)
    frames.push([isSpeech, evidence.voicedFraction, evidence.tonalConcentration, evidence.spectralFlatness])
  },
}
const control = attachIdleVadReset(vad, error => {throw error}, () => {analyzer.reset(); resets.push(end)})
try {
  for (let offset = 0; offset < pcm.length; offset += 3072) {
    end = offset / 2 + 1536
    const frame = Float32Array.from({length: 1536}, (_, i) => pcm.readInt16LE(offset + i * 2) / 32768)
    await vad.processFrame(frame)
  }
  console.log(JSON.stringify({frames, events, resets}))
} finally {
  await control.close()
  analyzer.close()
  await model.release()
}
