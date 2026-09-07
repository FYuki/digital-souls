// 実Sileroと製品の静音resetを使い、前の発話・待機後のラベル音声を全件診断する。
// PCMの仮想clockによる診断であり、Browser実接続の受け入れ結果とは区別する。
import {readFile, writeFile} from 'node:fs/promises'
import {createRequire} from 'node:module'
import {createHash} from 'node:crypto'
import {resolve, dirname} from 'node:path'
import {fileURLToPath} from 'node:url'
import {transform} from 'esbuild'
const root=resolve(dirname(fileURLToPath(import.meta.url)), '..')
const require=createRequire(root+'/package.json')
const {SileroLegacy,SileroV5}=require('@ricky0123/vad-web/dist/models')
const {Resampler}=require('@ricky0123/vad-web/dist/resampler')
const ort=require('onnxruntime-web/wasm');ort.env.wasm.numThreads=1
if(!process.argv[2])throw Error('output path required')
const output=resolve(process.argv[2]), modelName=process.argv[3]??'legacy'
if(!['legacy','v5'].includes(modelName))throw Error('invalid model')
const frameMs=modelName==='legacy'?96:32
const hash=bytes=>createHash('sha256').update(bytes).digest('hex')
const source=await readFile(process.argv[4]?resolve(process.argv[4]):root+'/src/lib/audio/utterance-detector.ts','utf8')
const idleSource=await readFile(root+'/src/lib/audio/idle-vad-reset.ts','utf8')
const moduleFrom=async source=>import('data:text/javascript;base64,'+Buffer.from((await transform(source,{loader:'ts',format:'esm'})).code).toString('base64'))
const {UtteranceDetector}=await moduleFrom(source)
const {attachIdleVadReset,idleVadResetOptions}=await moduleFrom(idleSource)
function pcm(bytes) {
 let data,rate,channels,bits,format
 for(let offset=12;offset+8<=bytes.length;) {
  const size=bytes.readUInt32LE(offset+4),kind=bytes.toString('ascii',offset,offset+4)
  if(offset+8+size>bytes.length)throw Error('truncated WAV')
  if(kind==='fmt ') {format=bytes.readUInt16LE(offset+8);rate=bytes.readUInt32LE(offset+12);channels=bytes.readUInt16LE(offset+10);bits=bytes.readUInt16LE(offset+22)}
  if(kind==='data')data=bytes.subarray(offset+8,offset+8+size)
  offset+=8+size+size%2
 }
 if(!data||format!==1||rate!==48000||channels!==1||bits!==16)throw Error('48kHz mono PCM16 required')
 return Float32Array.from({length:data.length/2},(_,i)=>data.readInt16LE(i*2)/32768)
}
const initialBytes=await readFile(root+'/playwright/fixtures/speech.wav'),initial=pcm(initialBytes)
const manifestBytes=await readFile(root+'/playwright/fixtures/voice-quality-v2/manifest.json')
const fixtures=JSON.parse(manifestBytes).trials
const modelBytes=await readFile(root+`/node_modules/@ricky0123/vad-web/dist/silero_vad_${modelName}.onnx`)
const model=await (modelName==='legacy'?SileroLegacy:SileroV5).new(ort,async()=>modelBytes.buffer.slice(modelBytes.byteOffset,modelBytes.byteOffset+modelBytes.byteLength))
const trials=[]
try {
 for(const [index,fixture] of fixtures.entries()) {
  const fixtureBytes=await readFile(root+`/test-results/vad-quality/fixtures-v2/${fixture.id}.wav`)
  if(hash(fixtureBytes)!==fixture.audio_sha256)throw Error('fixture mismatch')
  const audio=pcm(fixtureBytes),phaseMs=(index%6)*16,gapMs=2000
  const targetStart=initial.length+gapMs*48+phaseMs*48
  const input=new Float32Array(targetStart+audio.length)
  input.set(initial);input.set(audio,targetStart)
  const events=[],detector=new UtteranceDetector(e=>events.push(e)),resetTimes=[]
  let atMs=0,processingError=null
  model.reset_state()
  const vad={pause:async()=>{model.reset_state();resetTimes.push(atMs)},start:async()=>{},processFrame:async frame=>{
   const p=await model.process(frame);detector.process(frame,p.isSpeech,atMs)
  }}
  const control=attachIdleVadReset(vad,error=>{processingError=error})
  const resampler=new Resampler({nativeSampleRate:48000,targetSampleRate:16000,targetFrameSize:frameMs*16})
  for await(const frame of resampler.stream(input)) {
   atMs+=frameMs;await vad.processFrame(frame)
   if(processingError)throw processingError
  }
  await control.close()
  const targetEvents=events.filter(e=>e.detectedAtMs>targetStart/48)
   .map(e=>({...e,speechStartedAtMs:e.speechStartedAtMs-targetStart/48,detectedAtMs:e.detectedAtMs-targetStart/48}))
  const ends=targetEvents.filter(e=>e.type==='ended'),confirmed=targetEvents.filter(e=>e.type==='confirmed')
  const start=fixture.speech_intervals[0].start_sample/48,end=fixture.speech_intervals.at(-1).end_sample/48
  trials.push({fixture_sha256:fixture.audio_sha256,cohort:fixture.cohort,phase_ms:phaseMs,gap_ms:gapMs,
   initial_confirmed:events.filter(e=>e.type==='confirmed'&&e.detectedAtMs<=targetStart/48).length,
   missed:ends.length===0,leading_loss:!confirmed.length||confirmed[0].speechStartedAtMs-start>100,
   early_end:ends.some(e=>end-e.detectedAtMs>100),split:ends.length>1,
   finalize_delay_ms:ends.length===1?ends[0].detectedAtMs-end:null,
   reset_count:resetTimes.length,target_events:targetEvents})
 }
}finally{await model.release()}
const p95=values=>{if(!values.length)return null;values.sort((a,b)=>a-b);const i=(values.length-1)*.95;return values[Math.floor(i)]+(values[Math.ceil(i)]-values[Math.floor(i)])*(i%1)}
const summary=Object.fromEntries(['backchannel','take_turn','pause'].map(cohort=>{
 const rows=trials.filter(t=>t.cohort===cohort)
 return [cohort,{denominator:rows.length,...Object.fromEntries(['missed','leading_loss','early_end','split'].map(k=>[k,rows.filter(t=>t[k]).length])),
  finalize_delay_valid:rows.filter(t=>t.finalize_delay_ms!==null).length,
  finalize_delay_p95_ms:p95(rows.flatMap(t=>t.finalize_delay_ms===null?[]:[t.finalize_delay_ms]))}]
}))
await writeFile(output,JSON.stringify({scope:'sequential_fixed_pcm_with_production_idle_reset_diagnostic',model:modelName,frame_ms:frameMs,
 script_sha256:hash(await readFile(fileURLToPath(import.meta.url))),idle_reset_options:idleVadResetOptions,idle_reset_sha256:hash(idleSource),
 model_sha256:hash(modelBytes),detector_sha256:hash(source),initial_fixture_sha256:hash(initialBytes),manifest_sha256:hash(manifestBytes),summary,trials},null,2)+'\n',{flag:'wx'})
console.log(JSON.stringify({summary,output}))
