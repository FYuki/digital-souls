// 実ブラウザのglobal clockとAudioParam automationを比較する。音声本文は保存しない。
import {createRequire} from 'node:module'
import {readFile, mkdir, writeFile} from 'node:fs/promises'
import {dirname, resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const require = createRequire(resolve(root, 'frontend/package.json'))
const {chromium} = require('@playwright/test')
const ts = require('typescript')
const output = process.argv[2]
if (!output) throw new Error('output path is required')
const source = await readFile(resolve(root, 'frontend/src/livekit/render-quantum-clock.ts'), 'utf8')
const code = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ESNext}}).outputText
const {renderQuantumClockSource} = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))
const worklet = renderQuantumClockSource + `
class ClockProbe extends AudioWorkletProcessor {
  static get parameterDescriptors() {return [{name:'seconds', defaultValue:0, automationRate:'a-rate'}];}
  constructor() {super(); this.clock=new RenderQuantumClock(); this.count=0; this.stale=0; this.mismatch=0; this.maximumError=0; this.examples=[];}
  process(_inputs, outputs, parameters) {
    const out=outputs[0][0]; out.fill(0);
    // 0〜64秒の線形automationはFloat32丸め込みでも48kHzの半sample未満。
    const independent=parameters.seconds[0]*48000;
    const result=this.clock.read(currentFrame,out.length);
    this.count++;
    if (result) {
      const error=Math.abs(result.frame-independent); this.maximumError=Math.max(this.maximumError,error);
      if (error>.5) this.mismatch++;
      if (!result.confirmed) {
        this.stale++;
        if (this.examples.length<20) this.examples.push({reported:currentFrame, counted:result.frame, automation:independent});
      }
    }
    if (this.count%128===0) this.port.postMessage({count:this.count, stale:this.stale, mismatch:this.mismatch, maximumError:this.maximumError, examples:this.examples});
    return true;
  }
}
registerProcessor('clock-probe',ClockProbe);`
const browser = await chromium.launch({headless:true, args:['--autoplay-policy=no-user-gesture-required']})
try {
  const page = await browser.newPage()
  await page.route('http://localhost:17991/**', route => route.fulfill({contentType:'text/html', body:'<!doctype html><title>音声時計診断</title>'}))
  await page.goto('http://localhost:17991/')
  const result = await page.evaluate(async source => {
    const context=new AudioContext({sampleRate:48000}); await context.suspend()
    const url=URL.createObjectURL(new Blob([source],{type:'text/javascript'}))
    await context.audioWorklet.addModule(url); URL.revokeObjectURL(url)
    const node=new AudioWorkletNode(context,'clock-probe',{numberOfInputs:0,numberOfOutputs:1,outputChannelCount:[1]})
    const clock=node.parameters.get('seconds'); clock.setValueAtTime(0,0);clock.linearRampToValueAtTime(64,64)
    let last=null;let errors=0;let mutations=0
    node.port.onmessage=event=>{last=event.data};node.onprocessorerror=()=>{errors++}
    node.connect(context.destination);await context.resume()
    // 診断用のgraph変更でTryLock競合を増やす。アプリや共有サービスは変更しない。
    const timer=setInterval(()=>{for(let i=0;i<25;i++){const gain=context.createGain();gain.connect(context.destination);gain.disconnect();context.getOutputTimestamp();mutations++}},1)
    await new Promise(resolve=>setTimeout(resolve,20000))
    clearInterval(timer);await context.suspend();const timestamp=context.getOutputTimestamp();
    node.disconnect();await context.close();return {last, errors, mutations, timestamp, contextClosed:context.state==='closed'}
  },worklet)
  const report={scope:'render_clock_diagnostic',browser:browser.version(),...result}
  await mkdir(dirname(output),{recursive:true});await writeFile(output,JSON.stringify(report,null,2)+'\n',{flag:'wx'})
  console.log(JSON.stringify(report))
  if (!result.contextClosed || result.errors || !result.last || result.last.mismatch || !result.last.stale) process.exitCode=1
} finally {await browser.close()}
