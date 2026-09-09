// 補助VADあり/なしを同じ音声確率と電子音・雑音216条件で比較するオフライン診断。
import {readFile,writeFile} from 'node:fs/promises'
import {createRequire} from 'node:module'
import {createHash} from 'node:crypto'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {createProductionShortSpeechAnalyzer, shortSpeechProvenance} from './load-short-speech-analyzer.mjs'
const root=resolve(dirname(fileURLToPath(import.meta.url)), '..')
if (!process.argv[2]) throw Error('output path required')
const require=createRequire(root+'/package.json'),{transform}=require('esbuild')
const {SileroLegacy}=require('@ricky0123/vad-web/dist/models')
const ort=require('onnxruntime-web/wasm');ort.env.wasm.numThreads=1
const load=async path=>{const source=await readFile(path,'utf8');return import('data:text/javascript;base64,'+Buffer.from((await transform(source,{loader:'ts',format:'esm'})).code).toString('base64'))}
const Baseline=(await load(root+'/src/lib/audio/utterance-detector.ts')).UtteranceDetector
const Candidate=Baseline
const {attachIdleVadReset}=await load(root+'/src/lib/audio/idle-vad-reset.ts')
const modelBytes=await readFile(root+'/node_modules/@ricky0123/vad-web/dist/silero_vad_legacy.onnx')
const model=await SileroLegacy.new(ort,async()=>modelBytes.buffer.slice(modelBytes.byteOffset,modelBytes.byteOffset+modelBytes.byteLength))
const secondary=await createProductionShortSpeechAnalyzer()
const rows=[]
try{
for(const kind of ['tone','two_tones','noise'])for(const durationMs of [200,350,500,650])for(const frequency of [220,440,1000])for(const phase of [0,16,32,48,64,80]){
  const samples=new Float32Array(16000*4),start=Math.round((300+phase)*16),end=start+durationMs*16
  let seed=frequency+durationMs+phase
  for(let i=start;i<end;i++){
    const t=(i-start)/16000
    seed=(Math.imul(seed,1664525)+1013904223)>>>0
    samples[i]=kind==='tone'?.03*Math.sin(2*Math.PI*frequency*t):kind==='two_tones'?.015*(Math.sin(2*Math.PI*frequency*t)+Math.sin(2*Math.PI*frequency*4/3*t)):.03*(seed/4294967296*2-1)
  }
  const baseline=[],candidate=[],a=new Baseline(e=>baseline.push(e)),b=new Candidate(e=>candidate.push(e))
  model.reset_state();secondary.reset();let atMs=0
  const vad={pause:async()=>model.reset_state(),start:async()=>{},processFrame:async frame=>{
    const p=await model.process(frame);a.process(frame,p.isSpeech,atMs);b.process(frame,p.isSpeech,atMs,secondary.process(frame))
  }}
  const control=attachIdleVadReset(vad,e=>{throw e},()=>secondary.reset())
  for(let offset=0;offset+1536<=samples.length;offset+=1536){atMs=(offset+1536)/16;await vad.processFrame(samples.slice(offset,offset+1536))}
  await control.close()
  rows.push({kind,duration_ms:durationMs,frequency,phase_ms:phase,fixture_sha256:createHash('sha256').update(new Uint8Array(samples.buffer)).digest('hex'),baseline_confirmed:baseline.filter(e=>e.type==='confirmed').length,candidate_confirmed:candidate.filter(e=>e.type==='confirmed').length})
}
}finally{secondary.close();await model.release()}
const summary={denominator:rows.length,baseline_false_starts:rows.filter(r=>r.baseline_confirmed).length,candidate_false_starts:rows.filter(r=>r.candidate_confirmed).length,new_false_starts:rows.filter(r=>r.candidate_confirmed&&!r.baseline_confirmed).length}
const hash=bytes=>createHash('sha256').update(bytes).digest('hex')
await writeFile(resolve(process.argv[2]),JSON.stringify({scope:'supplemental_short_non_speech_regression',short_speech:shortSpeechProvenance,model_sha256:hash(modelBytes),detector_sha256:hash(await readFile(root+'/src/lib/audio/utterance-detector.ts')),script_sha256:hash(await readFile(fileURLToPath(import.meta.url))),summary,rows},null,2)+'\n',{flag:'wx'})
console.log(JSON.stringify(summary))
