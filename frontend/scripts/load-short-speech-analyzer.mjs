// 製品の補助VADをNodeの固定PCM診断にも使い、別実装との乖離を防ぐ。
import {readFile} from 'node:fs/promises'
import {createHash} from 'node:crypto'
import {transform} from 'esbuild'

const source = await readFile(new URL('../src/lib/audio/short-speech-evidence.ts', import.meta.url), 'utf8')
const binary = await readFile(new URL('../src/lib/audio/vendor/libfvad.wasm', import.meta.url))
const transformed = await transform(source, {loader: 'ts', format: 'esm'})
const {createShortSpeechAnalyzer} = await import('data:text/javascript;base64,' + Buffer.from(transformed.code).toString('base64'))
const hash = bytes => createHash('sha256').update(bytes).digest('hex')
export const shortSpeechProvenance = {source_sha256: hash(source), wasm_sha256: hash(binary)}
export const createProductionShortSpeechAnalyzer = () => createShortSpeechAnalyzer(
  binary.buffer.slice(binary.byteOffset, binary.byteOffset + binary.byteLength),
)
