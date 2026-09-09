// 入力PCMの活動開始へモデルframeを揃える診断。製品実装・Browser実接続ではない。
import {readFile, writeFile} from 'node:fs/promises'
import {createRequire} from 'node:module'
import {createHash} from 'node:crypto'
import {resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
const root=resolve(process.argv[2]), output=resolve(process.argv[3])
const require=createRequire(root+'/package.json')
const {transform}=require('esbuild'),{SileroLegacy}=require('@ricky0123/vad-web/dist/models')
const ort=require('onnxruntime-web/wasm');ort.env.wasm.numThreads=1
const source=await readFile(root+'/src/lib/audio/utterance-detector.ts','utf8')
const {code}=await transform(source,{loader:'ts',format:'esm'})
const {UtteranceDetector}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))
const modelBytes=await readFile(root+'/node_modules/@ricky0123/vad-web/dist/silero_vad_legacy.onnx')
const model=await SileroLegacy.new(ort,async()=>modelBytes.buffer.slice(modelBytes.byteOffset,modelBytes.byteOffset+modelBytes.byteLength))
const hash=data=>createHash('sha256').update(data).digest('hex')
const rms=frame=>Math.sqrt(frame.reduce((s,v)=>s+v*v,0)/frame.length)
function wav(bytes){
 let data,rate,channels,bits,format
 for(let offset=12;offset+8<=bytes.length;){
  const size=bytes.readUInt32LE(offset+4), kind=bytes.toString('ascii',offset,offset+4)
  if(offset+8+size>bytes.length)throw Error('truncated WAV')
  if(kind==='fmt '){format=bytes.readUInt16LE(offset+8);channels=bytes.readUInt16LE(offset+10);rate=bytes.readUInt32LE(offset+12);bits=bytes.readUInt16LE(offset+22)}
  if(kind==='data')data=bytes.subarray(offset+8,offset+8+size)
  offset+=8+size+size%2
 }
 if(!data||rate!==48000||channels!==1||bits!==16||format!==1)throw Error('48k mono PCM16 required')
 return Float32Array.from({length:data.length/2},(_,i)=>data.readInt16LE(i*2)/32768)
}
async function measure(samples, mode){
 const events=[],frames=[],detector=new UtteranceDetector(e=>events.push(e))
 model.reset_state()
 let cursor=0,quietMs=0,sinceReset=256,armed=mode==='aligned',resets=0
 while(cursor+1536<=samples.length){
  if(armed){
   while(cursor+64<=samples.length && rms(samples.subarray(cursor,cursor+64))<.001)cursor+=64
   if(cursor+64>samples.length)break
   // 正解境界を参照せず、4ms活動窓の32ms前から次のモデルframeを開始する。
   cursor=Math.max(0,cursor-512);model.reset_state();resets++;quietMs=0;armed=false
   if(cursor+1536>samples.length)break
  }
  const frame=samples.slice(cursor,cursor+1536),quiet=rms(frame)<.001
  quietMs=quiet?quietMs+96:0;sinceReset+=96
  if(mode==='baseline'&&quietMs>=700&&sinceReset>=256){model.reset_state();resets++;sinceReset=0}
  const probability=(await model.process(frame)).isSpeech
  const atMs=(cursor+1536)/16
  detector.process(frame,probability,atMs)
  frames.push({end_sample:cursor+1536,probability,rms:rms(frame)})
  cursor+=1536
  if(mode==='aligned'&&quietMs>=700)armed=true
 }
 return {events,resets,maximum_probability:Math.max(0,...frames.map(f=>f.probability))}
}
const manifestBytes=await readFile(root+'/playwright/fixtures/voice-quality-v2/manifest.json')
const initialBytes=await readFile(root+'/playwright/fixtures/speech.wav'),initial=wav(initialBytes)
const fixtures=JSON.parse(manifestBytes).trials, trials=[]
try{
 for(const [index,fixture] of fixtures.entries()){
  const bytes=await readFile(root+'/test-results/vad-quality/fixtures-v2/'+fixture.id+'.wav')
  if(hash(bytes)!==fixture.audio_sha256)throw Error('fixture hash mismatch')
  const audio=wav(bytes),phaseMs=(index%6)*16,targetStart=initial.length+96000+phaseMs*48
  const pcm48=new Float32Array(targetStart+audio.length);pcm48.set(initial);pcm48.set(audio,targetStart)
  // 先行音声・静音・対象音声を連結してから、製品Resamplerと同じ整数比で平均する。
  const input=Float32Array.from({length:Math.floor(pcm48.length/3)},(_,i)=>(pcm48[i*3]+pcm48[i*3+1]+pcm48[i*3+2])/3)
  for(const mode of ['baseline','aligned']){
   const result=await measure(input,mode),events=result.events.filter(e=>e.detectedAtMs>targetStart/48).map(e=>({...e,speechStartedAtMs:e.speechStartedAtMs-targetStart/48,detectedAtMs:e.detectedAtMs-targetStart/48}))
   const ends=events.filter(e=>e.type==='ended'),confirmed=events.filter(e=>e.type==='confirmed')
   const start=fixture.speech_intervals[0].start_sample/48,end=fixture.speech_intervals.at(-1).end_sample/48
   trials.push({mode,cohort:fixture.cohort,fixture_sha256:fixture.audio_sha256,phase_ms:phaseMs,
    initial_confirmed:result.events.filter(e=>e.type==='confirmed'&&e.detectedAtMs<=targetStart/48).length,
    missed:ends.length===0,leading_loss:!confirmed.length||confirmed[0].speechStartedAtMs-start>100,
    early_end:ends.some(e=>end-e.detectedAtMs>100),split:ends.length>1,events,
    finalize_delay_ms:ends.length===1?ends[0].detectedAtMs-end:null})
  }
 }
 for(const kind of ['silence','white_noise','pink_noise','hum','tone','two_tones','click','keyboard','rustle','wind','short_tone','short_noise']){
  for(let variation=0;variation<10;variation++){
   const samples=new Float32Array(16000*6);let state=907+variation*53,smooth=0
   const random=()=>{state=(Math.imul(state,1664525)+1013904223)>>>0;return state/4294967296*2-1}
   for(let i=0;i<samples.length;i++){
    const t=i/16000,noise=random(),gain=.5+variation/10;smooth=.98*smooth+.02*noise;let value=0
    if(kind==='white_noise')value=noise*.015
    else if(kind==='pink_noise')value=(smooth*5+noise*.1)*.02
    else if(kind==='hum')value=Math.sin(2*Math.PI*(variation%2?50:60)*t)*.02
    else if(kind==='tone')value=Math.sin(2*Math.PI*440*t)*.05
    else if(kind==='two_tones')value=(Math.sin(2*Math.PI*330*t)+Math.sin(2*Math.PI*440*t))*.025
    else if(kind==='click')value=i===16000+variation*16?.5:0
    else if(kind==='keyboard')value=i%8000<160?noise*.1:0
    else if(kind==='rustle')value=noise*.02*Math.max(0,Math.sin(Math.PI*t))
    else if(kind==='wind')value=smooth*.2
    else if(kind==='short_tone')value=t>=.3&&t<1?Math.sin(2*Math.PI*440*t)*.05:0
    else if(kind==='short_noise')value=t>=.3&&t<.7?noise*.02:0
    if(t>=4)value=0;samples[i]=value*gain
   }
   for(const mode of ['baseline','aligned']){
    const result=await measure(samples,mode)
    trials.push({mode,cohort:'non_speech',kind,fixture_sha256:hash(new Uint8Array(samples.buffer)),false_start:result.events.some(e=>e.type==='confirmed'),...result})
   }
  }
 }
}finally{await model.release()}
const summary={}
for(const mode of ['baseline','aligned'])for(const cohort of ['backchannel','take_turn','pause','non_speech']){
 const rows=trials.filter(t=>t.mode===mode&&t.cohort===cohort)
 summary[mode+'_'+cohort]={denominator:rows.length,...Object.fromEntries(['missed','leading_loss','early_end','split','false_start'].map(k=>[k,rows.filter(t=>t[k]).length]))}
}
await writeFile(output,JSON.stringify({scope:'offline_model_frame_onset_alignment_candidate',production_changed:false,
 alignment:{activity_window_ms:4,pre_onset_ms:32,maximum_quiet_rms:.001,reset_quiet_ms:700},
 script_sha256:hash(await readFile(fileURLToPath(import.meta.url))),detector_sha256:hash(source),model_sha256:hash(modelBytes),
 manifest_sha256:hash(manifestBytes),initial_fixture_sha256:hash(initialBytes),summary,trials},null,2)+'\n',{flag:'wx'})
console.log(JSON.stringify({summary,output}))
