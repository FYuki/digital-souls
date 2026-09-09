// 固定音声に継続する合成環境音を重ね、音声終了後もVADが閉じるか実Sileroで診断する。
import {readFile,writeFile,mkdir} from 'node:fs/promises'
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
const leadSeconds=Number(process.argv[3]??0)
if(!Number.isFinite(leadSeconds)||leadSeconds<0||leadSeconds>120)throw Error('background lead must be between 0 and 120 seconds')
const leadFrames=Math.ceil(leadSeconds*1000/96),leadMs=leadFrames*96
const referenceSource=process.argv[4]?await readFile(resolve(process.argv[4]),'utf8'):null
const referenceCode=referenceSource===null?null:(await transform(referenceSource,{loader:'ts',format:'esm'})).code
const ReferenceDetector=referenceCode===null?null:(await import('data:text/javascript;base64,'+Buffer.from(referenceCode).toString('base64'))).UtteranceDetector
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
  const events=[],referenceEvents=[],detector=new UtteranceDetector(e=>events.push(e))
  const reference=ReferenceDetector?new ReferenceDetector(e=>referenceEvents.push(e)):null
  model.reset_state()
  for(let frameIndex=0;frameIndex<leadFrames+frames.length+32;frameIndex++) {
   const frame=new Float32Array(frames[frameIndex-leadFrames]??1536)
   for(let i=0;i<frame.length;i++,offset++) {
    state=(Math.imul(state,1664525)+1013904223)>>>0
    const r=state/4294967296*2-1;smooth=.98*smooth+.02*r
    const noise=kind==='white_noise'?r:kind==='hum'?Math.sin(2*Math.PI*60*offset/16000):(smooth*5+r*.1)
    frame[i]+=noise*gain
   }
   const p=await model.process(frame);detector.process(frame,p.isSpeech,offset/16);reference?.process(frame,p.isSpeech,offset/16)
  }
  const speechStart=leadMs+metadata.speech_start_sample*1000/rate,speechEnd=leadMs+metadata.speech_end_sample*1000/rate
  const summarize=observed=>{
   const confirmed=observed.filter(e=>e.type==='confirmed'),ends=observed.filter(e=>e.type==='ended')
   return {events:observed,ended:ends.length,confirmed_before_speech:confirmed.filter(e=>e.detectedAtMs<speechStart).length,
    confirmation_ages_ms:confirmed.map(e=>e.detectedAtMs-e.speechStartedAtMs),
    finalize_delay_ms:ends.map(e=>e.detectedAtMs-speechEnd)}
  }
  trials.push({kind,gain,...summarize(events),...(reference?{reference:summarize(referenceEvents)}:{})})
 }
}finally{await model.release()}
const report={lead_ms:leadMs,noise_seed:907,reference_detector_sha256:referenceSource===null?null:hash(referenceSource),scope:'fixed_speech_with_continuous_synthetic_background_real_silero_diagnostic',fixture_sha256:hash(bytes),model_sha256:hash(modelBytes),detector_sha256:hash(codeSource),trials}
const output=resolve(process.argv[2]??root+'/test-results/vad-quality/utterance-continuous-background-guarded.json')
await mkdir(dirname(output),{recursive:true})
await writeFile(output,JSON.stringify(report,null,2)+'\n',{flag:'wx'})
console.log(JSON.stringify(trials.map(({kind,gain,ended,finalize_delay_ms,confirmation_ages_ms,confirmed_before_speech,reference})=>({kind,gain,ended,finalize_delay_ms,confirmation_ages_ms,confirmed_before_speech,...(reference?{reference_confirmation_ages_ms:reference.confirmation_ages_ms,reference_ended:reference.ended}:{})}))))
