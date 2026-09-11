// 候補VADを無音・非発声の固定合成音100件へ通す。外部の録音素材は使わない。
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { transform } from 'esbuild'
import {createProductionShortSpeechAnalyzer, shortSpeechProvenance} from './load-short-speech-analyzer.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const require = createRequire(resolve(root, 'package.json'))
const {FrameProcessor, Message, getDefaultRealTimeVADOptions} = require('@ricky0123/vad-web')
const {SileroLegacy,SileroV5} = require('@ricky0123/vad-web/dist/models')
const ort=require('onnxruntime-web/wasm')
ort.env.wasm.numThreads=1
const source=await readFile(process.argv[3] ? resolve(process.argv[3]) : resolve(root,'src/lib/audio/utterance-detector.ts'),'utf8')
const {code}=await transform(source,{loader:'ts',format:'esm'})
const {UtteranceDetector,utteranceDetectorOptions}=await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
const modelName=process.argv[5]??'legacy'
if(!['legacy','v5'].includes(modelName))throw Error('invalid model')
const frameMs=modelName==='v5'?32:96
const frameSamples=frameMs*16
const modelBytes=await readFile(resolve(root,`node_modules/@ricky0123/vad-web/dist/silero_vad_${modelName}.onnx`))
const model=await (modelName==='v5'?SileroV5:SileroLegacy).new(ort,async()=>modelBytes.buffer.slice(modelBytes.byteOffset,modelBytes.byteOffset+modelBytes.byteLength))
const hash=bytes=>createHash('sha256').update(bytes).digest('hex')
const idleReset=process.argv[4]==='idle-reset'
if(process.argv[4]!==undefined&&!idleReset)throw Error('invalid reset mode')
const idleSource=idleReset?await readFile(resolve(root,'src/lib/audio/idle-vad-reset.ts'),'utf8'):null
const idleModule=idleSource===null?null:await transform(idleSource,{loader:'ts',format:'esm'})
const attachIdleReset=idleModule===null?null:(await import(`data:text/javascript;base64,${Buffer.from(idleModule.code).toString('base64')}`)).attachIdleVadReset
const secondary = modelName === 'legacy' ? await createProductionShortSpeechAnalyzer() : null
const trials=[]
try {
 for(const kind of ['silence','white_noise','pink_noise','hum','tone','two_tones','click','keyboard','rustle','wind','short_tone','short_noise']) {
  for(let variation=0;variation<10;variation++) {
   const samples=new Float32Array(16000*6)
   let state=907+variation*53, smooth=0
   const random=()=>{state=(Math.imul(state,1664525)+1013904223)>>>0; return state/4294967296*2-1}
   for(let i=0;i<samples.length;i++) {
    const t=i/16000, noise=random(), gain=.5+variation/10
    smooth=.98*smooth+.02*noise
    let value=0
    if(kind==='white_noise') value=noise*.015
    else if(kind==='pink_noise') value=(smooth*5+noise*.1)*.02
    else if(kind==='hum') value=Math.sin(2*Math.PI*(variation%2?50:60)*t)*.02
    else if(kind==='tone') value=Math.sin(2*Math.PI*440*t)*.05
    else if(kind==='two_tones') value=(Math.sin(2*Math.PI*330*t)+Math.sin(2*Math.PI*440*t))*.025
    else if(kind==='click') value=i===16000+variation*16?.5:0
    else if(kind==='keyboard') value=i%8000<160?noise*.1:0
    else if(kind==='rustle') value=noise*.02*Math.max(0,Math.sin(Math.PI*t))
    else if(kind==='wind') value=smooth*.2
    else if(kind==='short_tone') value=t>=.3&&t<1?Math.sin(2*Math.PI*440*t)*.05:0
    else if(kind==='short_noise') value=t>=.3&&t<.7?noise*.02:0
    if (t>=4) value=0
    samples[i]=value*gain
   }
   const events=[]
   const detector=new UtteranceDetector(event=>events.push(event))
   let currentProbability=0
   const baselineEvents=[]
   const baseline=new FrameProcessor(async()=>({isSpeech:currentProbability,notSpeech:1-currentProbability}),()=>{},
    {...getDefaultRealTimeVADOptions(modelName),redemptionMs:700},frameMs)
   baseline.resume(); model.reset_state(); secondary?.reset()
   let maxProbability=0
   const frameProbabilities=[]
   let atMs=0
   const vad={pause:async()=>model.reset_state(),start:async()=>{},processFrame:async frame=>{
    const probability=await model.process(frame)
    currentProbability=probability.isSpeech
    frameProbabilities.push(currentProbability)
    maxProbability=Math.max(maxProbability,currentProbability)
    detector.process(frame,currentProbability,atMs,secondary?.process(frame))
    await baseline.process(frame,event=>{if(event.msg===Message.SpeechRealStart)baselineEvents.push(atMs)})
   }}
   const control=attachIdleReset?.(vad,error=>{throw error},()=>secondary?.reset())
   for(let offset=0;offset+frameSamples<=samples.length;offset+=frameSamples) {
    atMs=(offset+frameSamples)/16
    await vad.processFrame(samples.slice(offset,offset+frameSamples))
   }
   await control?.close()
   trials.push({kind,fixture_sha256:hash(new Uint8Array(samples.buffer)),max_probability:maxProbability,
    candidate_false_start:events.some(event=>event.type==='confirmed'),baseline_false_start:baselineEvents.length>0,
    frame_probabilities:frameProbabilities,events})
  }
 }
} finally {secondary?.close();await model.release()}
const summary={denominator:trials.length,candidate_false_starts:trials.filter(t=>t.candidate_false_start).length,
 baseline_false_starts:trials.filter(t=>t.baseline_false_start).length}
const output=resolve(process.argv[2] ?? resolve(root,'test-results/vad-quality/utterance-background-v1.json'))
await mkdir(dirname(output),{recursive:true})
await writeFile(output,JSON.stringify({scope:'synthetic_non_speech_real_silero_diagnostic',short_speech:secondary ? shortSpeechProvenance : null,detector_sha256:hash(source),
 idle_reset:idleReset,idle_reset_source_sha256:idleSource===null?null:hash(idleSource),model_sha256:hash(modelBytes),model:modelName,frame_ms:frameMs,options:utteranceDetectorOptions,summary,trials},null,2)+'\n',{flag:'wx'})
console.log(JSON.stringify({summary,output}))
