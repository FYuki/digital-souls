// 同じ音声について前の発話・待機時間・frame位相・モデル状態resetの影響を分離する診断。
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
const output=resolve(process.argv[2])
const modelName=process.argv[3]??'legacy'
if(!['legacy','v5'].includes(modelName))throw Error('invalid model')
const frameMs=modelName==='legacy'?96:32
const hash=bytes=>createHash('sha256').update(bytes).digest('hex')
const scriptSha=hash(await readFile(fileURLToPath(import.meta.url)))
const source=await readFile(process.argv[4]?resolve(process.argv[4]):root+'/src/lib/audio/utterance-detector.ts','utf8')
const {code}=await transform(source,{loader:'ts',format:'esm'})
const {UtteranceDetector}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
function pcm(bytes) {
 let data,rate,channels,bits
 for(let offset=12;offset+8<=bytes.length;) {
  const size=bytes.readUInt32LE(offset+4),kind=bytes.toString('ascii',offset,offset+4)
  if(offset+8+size>bytes.length)throw Error('truncated WAV')
  if(kind==='fmt ') {rate=bytes.readUInt32LE(offset+12);channels=bytes.readUInt16LE(offset+10);bits=bytes.readUInt16LE(offset+22)}
  if(kind==='data')data=bytes.subarray(offset+8,offset+8+size)
  offset+=8+size+size%2
 }
 if(!data||rate!==48000||channels!==1||bits!==16)throw Error('48kHz mono PCM16 required')
 return Float32Array.from({length:data.length/2},(_,i)=>data.readInt16LE(i*2)/32768)
}
const initialBytes=await readFile(root+'/playwright/fixtures/speech.wav')
const initial=pcm(initialBytes)
const manifestBytes=await readFile(root+'/playwright/fixtures/voice-quality-v2/manifest.json')
const selected=JSON.parse(manifestBytes).trials.filter(t=>t.cohort==='take_turn').slice(0,4)
const bytes=await readFile(root+`/node_modules/@ricky0123/vad-web/dist/silero_vad_${modelName}.onnx`)
const model=await (modelName==='legacy'?SileroLegacy:SileroV5).new(ort,async()=>bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength))
const trials=[]
try {
 for(const fixture of selected) {
  const fixtureBytes=await readFile(root+`/test-results/vad-quality/fixtures-v2/${fixture.id}.wav`)
  if(hash(fixtureBytes)!==fixture.audio_sha256)throw Error('fixture mismatch')
  const audio=pcm(fixtureBytes)
  for(const phaseMs of [0,16,32,48,64,80]) {
   for(const gapMs of [0,2000,10000])for(const mode of ['continuous','reset_before_target','fresh','quiet_periodic_reset','quiet_after_frame','quiet_onset_reset']) {
    if(mode==='fresh'&&gapMs!==0)continue
    const prefix=mode==='fresh'?0:initial.length+gapMs*48
    const targetStart=prefix+phaseMs*48
    const input=new Float32Array(targetStart+audio.length)
    if(mode!=='fresh')input.set(initial)
    input.set(audio,targetStart)
    const resampler=new Resampler({nativeSampleRate:48000,targetSampleRate:16000,targetFrameSize:frameMs*16})
    const events=[],detector=new UtteranceDetector(e=>events.push(e))
    const probabilities=[]
    model.reset_state()
    let sampleOffset=0,reset=false,quietMs=0,lastResetMs=-Infinity
    for await(const frame of resampler.stream(input)) {
     const atMs=(sampleOffset+frame.length)/16
     if(mode==='reset_before_target'&&!reset&&atMs>targetStart/48) {model.reset_state();reset=true}
     let energy=0;for(const sample of frame)energy+=sample*sample
     const quiet=Math.sqrt(energy/frame.length)<.001
     if(mode==='quiet_onset_reset'&&!quiet&&quietMs>=700)model.reset_state()
     quietMs=quiet?quietMs+frameMs:0
     if(mode==='quiet_periodic_reset'&&quietMs>=700&&atMs-lastResetMs>=256) {
      model.reset_state();lastResetMs=atMs
     }
     const p=await model.process(frame)
     detector.process(frame,p.isSpeech,atMs)
     if(mode==='quiet_after_frame'&&quietMs>=700&&atMs-lastResetMs>=256) {
      model.reset_state();lastResetMs=atMs
     }
     if(atMs>targetStart/48)probabilities.push(p.isSpeech)
     sampleOffset+=frame.length
    }
    const targetEvents=events.filter(e=>e.detectedAtMs>targetStart/48)
    trials.push({fixture_sha256:fixture.audio_sha256,phase_ms:phaseMs,target_frame_phase_ms:(targetStart/48)%frameMs,gap_ms:gapMs,mode,
      maximum_probability:Math.max(...probabilities),
      confirmed:targetEvents.some(e=>e.type==='confirmed'),ended:targetEvents.some(e=>e.type==='ended'),
      target_events:targetEvents.map(e=>({...e,speechStartedAtMs:e.speechStartedAtMs-targetStart/48,detectedAtMs:e.detectedAtMs-targetStart/48}))})
   }
  }
 }
}finally{await model.release()}
const summary=Object.fromEntries(['fresh','continuous','reset_before_target','quiet_periodic_reset','quiet_after_frame','quiet_onset_reset'].map(mode=>{
 const rows=trials.filter(t=>t.mode===mode)
 return [mode,{trials:rows.length,confirmed:rows.filter(t=>t.confirmed).length,ended:rows.filter(t=>t.ended).length,
  minimum_max_probability:Math.min(...rows.map(t=>t.maximum_probability))}]
}))
await writeFile(output,JSON.stringify({scope:'fixed_pcm_vad_state_phase_diagnostic',model:modelName,frame_ms:frameMs,
 script_sha256:scriptSha,quiet_reset_policy:{minimum_quiet_ms:700,reset_interval_ms:256,maximum_rms:.001},model_sha256:hash(bytes),detector_sha256:hash(source),initial_fixture_sha256:hash(initialBytes),manifest_sha256:hash(manifestBytes),summary,trials},null,2)+'\n',{flag:'wx'})
console.log(JSON.stringify({summary,output}))
