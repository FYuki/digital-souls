// 固定音声に継続する合成環境音を重ね、音声終了後もVADが閉じるか実Sileroで診断する。
import {readFile,writeFile} from 'node:fs/promises'
import {createRequire} from 'node:module'
import {createHash} from 'node:crypto'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
const root=resolve(dirname(fileURLToPath(import.meta.url)), '..')
const require=createRequire(root+'/package.json')
const {transform}=require('esbuild'),{SileroLegacy}=require('@ricky0123/vad-web/dist/models'),{Resampler}=require('@ricky0123/vad-web/dist/resampler')
const ort=require('onnxruntime-web/wasm');ort.env.wasm.numThreads=1
const codeSource=await readFile(root+'/src/lib/audio/utterance-detector.ts','utf8')
const {code}=await transform(codeSource,{loader:'ts',format:'esm'})
const {UtteranceDetector}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
const hash=b=>createHash('sha256').update(b).digest('hex')
const bytes=await readFile(root+'/playwright/fixtures/speech.wav')
const metadata=JSON.parse(await readFile(root+'/playwright/fixtures/speech.metadata.json','utf8'))
if(hash(bytes)!==metadata.audio_sha256)throw Error('fixture mismatch')
let pcm,rate
for(let o=12;o+8<=bytes.length;) {const n=bytes.readUInt32LE(o+4),name=bytes.toString('ascii',o,o+4)
 if(name==='fmt ') {rate=bytes.readUInt32LE(o+12);if(bytes.readUInt16LE(o+10)!==1||bytes.readUInt16LE(o+22)!==16)throw Error('PCM format')}
 if(name==='data')pcm=bytes.subarray(o+8,o+8+n)
 o+=8+n+n%2
}
const input=Float32Array.from({length:pcm.length/2},(_,i)=>pcm.readInt16LE(i*2)/32768)
const resampler=new Resampler({nativeSampleRate:rate,targetSampleRate:16000,targetFrameSize:1536})
const frames=[];for await(const f of resampler.stream(input))frames.push(f)
const modelBytes=await readFile(root+'/node_modules/@ricky0123/vad-web/dist/silero_vad_legacy.onnx')
const model=await SileroLegacy.new(ort,async()=>modelBytes.buffer.slice(modelBytes.byteOffset,modelBytes.byteOffset+modelBytes.byteLength))
const trials=[]
try {
 for(const kind of ['white_noise','hum','pink_noise'])for(const gain of [.005,.015]) {
  let state=907,smooth=0,offset=0
  const events=[],detector=new UtteranceDetector(e=>events.push(e));model.reset_state()
  for(let frameIndex=0;frameIndex<frames.length+32;frameIndex++) {
   const frame=new Float32Array(frames[frameIndex]??1536)
   for(let i=0;i<frame.length;i++,offset++) {
    state=(Math.imul(state,1664525)+1013904223)>>>0
    const r=state/4294967296*2-1;smooth=.98*smooth+.02*r
    const noise=kind==='white_noise'?r:kind==='hum'?Math.sin(2*Math.PI*60*offset/16000):(smooth*5+r*.1)
    frame[i]+=noise*gain
   }
   const p=await model.process(frame);detector.process(frame,p.isSpeech,offset/16)
  }
  const ends=events.filter(e=>e.type==='ended')
  trials.push({kind,gain,events,ended:ends.length,finalize_delay_ms:ends.map(e=>e.detectedAtMs-metadata.speech_end_sample*1000/rate)})
 }
}finally{await model.release()}
const report={scope:'fixed_speech_with_continuous_synthetic_background_real_silero_diagnostic',fixture_sha256:hash(bytes),model_sha256:hash(modelBytes),detector_sha256:hash(codeSource),trials}
await writeFile(root+'/test-results/vad-quality/utterance-continuous-background-guarded.json',JSON.stringify(report,null,2)+'\n')
console.log(JSON.stringify(trials.map(({kind,gain,ended,finalize_delay_ms})=>({kind,gain,ended,finalize_delay_ms}))))
