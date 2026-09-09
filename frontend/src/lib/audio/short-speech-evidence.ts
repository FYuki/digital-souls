// 短い未確定音声の補助根拠。主VADの確率や録音PCMを変更しない。
export type ShortSpeechEvidence = Readonly<{
  voicedFraction: number
  tonalConcentration: number
  spectralFlatness: number
}>
export type ShortSpeechAnalyzer = {
  process: (frame: Float32Array) => ShortSpeechEvidence
  reset: () => void
  close: () => void
}

type NativeVad = {
  memory: WebAssembly.Memory
  malloc: (size: number) => number
  free: (pointer: number) => void
  fvad_new: () => number
  fvad_free: (handle: number) => void
  fvad_set_mode: (handle: number, mode: number) => number
  fvad_set_sample_rate: (handle: number, sampleRate: number) => number
  fvad_process: (handle: number, pointer: number, samples: number) => number
}

export async function createShortSpeechAnalyzer(binary?: ArrayBuffer): Promise<ShortSpeechAnalyzer> {
  if (binary === undefined) {
    const response = await fetch(new URL('./vendor/libfvad.wasm', import.meta.url))
    if (!response.ok) throw new Error('Short speech VAD asset unavailable')
    binary = await response.arrayBuffer()
  }
  const {instance} = await WebAssembly.instantiate(binary)
  const exports = instance.exports
  for (const name of ['malloc', 'free', 'fvad_new', 'fvad_free', 'fvad_set_mode', 'fvad_set_sample_rate', 'fvad_process']) {
    if (typeof exports[name] !== 'function') throw new Error('Invalid short speech VAD exports')
  }
  if (!(exports.memory instanceof WebAssembly.Memory)) throw new Error('Invalid short speech VAD memory')
  const native = exports as unknown as NativeVad
  const input = native.malloc(320)
  if (!input) throw new Error('Short speech VAD buffer allocation failed')
  let handle = 0
  let closed = false
  let pending = new Float32Array(0)
  const close = () => {
    if (closed) return
    closed = true
    if (handle) native.fvad_free(handle)
    handle = 0
    native.free(input)
    pending = new Float32Array(0)
  }
  const reset = () => {
    if (closed) throw new Error('Short speech VAD is closed')
    if (handle) native.fvad_free(handle)
    handle = 0
    pending = new Float32Array(0)
    try {
      handle = native.fvad_new()
      if (!handle || native.fvad_set_mode(handle, 0) !== 0 || native.fvad_set_sample_rate(handle, 16000) !== 0) {
        throw new Error('Short speech VAD initialization failed')
      }
    } catch (error) {
      close()
      throw error
    }
  }
  reset()
  return {
    reset,
    close,
    process(frame) {
      if (closed) throw new Error('Short speech VAD is closed')
      if (frame.length !== 1536 || !frame.every(Number.isFinite)) throw new Error('Invalid short speech VAD frame')
      const combined = new Float32Array(pending.length + frame.length)
      combined.set(pending)
      combined.set(frame, pending.length)
      let count = 0, voiced = 0, offset = 0
      for (; offset + 160 <= combined.length; offset += 160) {
        // WASMのmemory拡張後も現在のbufferを参照し、入力の端数は別途保持する。
        const samples = new Int16Array(native.memory.buffer, input, 160)
        for (let i = 0; i < 160; i++) {
          const value = Math.max(-1, Math.min(1, combined[offset + i]))
          samples[i] = value < 0 ? value * 32768 : value * 32767
        }
        const result = native.fvad_process(handle, input, 160)
        if (result !== 0 && result !== 1) throw new Error('Short speech VAD processing failed')
        count++
        voiced += result
      }
      pending = combined.slice(offset)
      return {voicedFraction: voiced / count, ...spectralEvidence(frame)}
    },
  }
}

// 1024点Hann窓で2つの5bin帯域への集中度と平坦度を測り、電子音と広帯域雑音を区別する。
export function spectralEvidence(samples: Float32Array): Pick<ShortSpeechEvidence, "tonalConcentration" | "spectralFlatness"> {
  if (samples.length < 1024 || !samples.every(Number.isFinite)) throw new Error("Invalid spectral evidence frame")
  const n=1024,re=new Float64Array(n),im=new Float64Array(n)
  for(let i=0;i<n;i++)re[i]=(samples[samples.length-n+i]??0)*(.5-.5*Math.cos(2*Math.PI*i/(n-1)))
  for(let i=1,j=0;i<n;i++){
    let bit=n>>1;for(;j&bit;bit>>=1)j^=bit;j^=bit
    if(i<j){const t=re[i];re[i]=re[j];re[j]=t}
  }
  for(let length=2;length<=n;length<<=1){
    const angle=-2*Math.PI/length,wr=Math.cos(angle),wi=Math.sin(angle)
    for(let offset=0;offset<n;offset+=length){
      let cr=1,ci=0
      for(let j=0;j<length/2;j++){
        const a=offset+j,b=a+length/2,br=re[b]*cr-im[b]*ci,bi=re[b]*ci+im[b]*cr
        re[b]=re[a]-br;im[b]=im[a]-bi;re[a]+=br;im[a]+=bi
        const nr=cr*wr-ci*wi;ci=cr*wi+ci*wr;cr=nr
      }
    }
  }
  const power=Float64Array.from({length:n/2},(_,i)=>re[i+1]**2+im[i+1]**2)
  const total=power.reduce((a,b)=>a+b,0)
  if(total===0)return {tonalConcentration:1,spectralFlatness:1}
  const floor=total*1e-12
  const spectralFlatness=Math.exp(power.reduce((sum,p)=>sum+Math.log(Math.max(p,floor)),0)/power.length)/(total/power.length)
  const remaining=power.slice()
  let concentration=0
  for(let pass=0;pass<2;pass++){
    let best=0,center=0
    for(let i=0;i<remaining.length;i++){
      let sum=0
      for(let j=Math.max(0,i-2);j<=Math.min(remaining.length-1,i+2);j++)sum+=remaining[j]
      if(sum>best){best=sum;center=i}
    }
    concentration+=best
    for(let j=Math.max(0,center-2);j<=Math.min(remaining.length-1,center+2);j++)remaining[j]=0
  }
  return {tonalConcentration:concentration/total,spectralFlatness}
}
