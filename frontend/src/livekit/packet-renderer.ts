// 音声本文はworker→workletだけへ渡し、mainの観測には数値とpacket番号を残す。
export const packetRendererSource = `
class PacketRenderer extends AudioWorkletProcessor {
  constructor(options) {
    super(); this.queue = []; this.offset = 0; this.stopped = false;
    this.paused = !!options.processorOptions?.paused;
    this.queuedSamples = 0; this.renderedSamples = 0; this.droppedSamples = 0;
    this.nextPacket = 0; this.started = false; this.startFrame = null;
    this.port.onmessage = ({data}) => {
      if (data.kind === 'stop') {
        this.stopped = true; this.droppedSamples += this.queuedSamples;
        this.queue = []; this.offset = 0; this.queuedSamples = 0; return;
      }
      if (data.kind === 'resume' && !this.stopped) { this.paused = false; return; }
      if (data.kind !== 'pcm') return;
      if (!(data.samples instanceof Float32Array) || data.samples.length !== 960
        || data.samples.some(value => !Number.isFinite(value))
        || data.packetIndex !== this.nextPacket) {
        this.port.postMessage({kind: 'error', reason: 'packet_or_sample_mismatch'}); return;
      }
      this.nextPacket++;
      if (this.stopped) {this.droppedSamples += data.samples.length; return;}
      if (this.queuedSamples + data.samples.length > 48000) {
        this.port.postMessage({kind: 'error', reason: 'pcm_queue_overflow'}); return;
      }
      this.queue.push(data); this.queuedSamples += data.samples.length;
      // 初回は60msを確保する。callbackの遅着で各packetの予定を後ろへずらさない。
      if (this.startFrame === null) this.startFrame = currentFrame + 2880;
    };
  }
  process(_inputs, outputs) {
    const out = outputs[0]?.[0];
    if (!out) return true;
    out.fill(0);
    if (this.stopped || this.paused || this.startFrame === null) return true;
    let target = Math.max(0, Math.min(out.length, this.startFrame - currentFrame));
    while (target < out.length && this.queue.length) {
      const chunk = this.queue[0], offset = this.offset;
      const count = Math.min(out.length - target, chunk.samples.length - offset);
      const samples = chunk.samples.subarray(offset, offset + count);
      out.set(samples, target);
      let energy = 0, audible;
      for (let i = 0; i < samples.length; i++) {
        energy += samples[i] * samples[i];
        if (audible === undefined && samples[i] !== 0) audible = currentFrame + target + i;
      }
      const startFrame = currentFrame + target, endFrame = startFrame + count;
      this.port.postMessage({kind: 'rendered', packetIndex: chunk.packetIndex,
        rtpTimestamp: chunk.rtpTimestamp, packetSampleOffset: offset,
        startFrame, endFrame, energy, ...(audible === undefined ? {} : {firstAudibleFrame: audible})});
      this.offset += count; this.queuedSamples -= count; this.renderedSamples += count; target += count;
      this.started = true;
      if (this.offset === chunk.samples.length) {this.queue.shift(); this.offset = 0;}
    }
    // この時点では末尾とunderrunの区別が未確定。空き区間をrawとして記録する。
    if (this.started && target < out.length) this.port.postMessage({kind: 'empty',
      startFrame: currentFrame + target, endFrame: currentFrame + out.length,
      renderedSamples: this.renderedSamples});
    return true;
  }
}
registerProcessor('packet-renderer', PacketRenderer);
`

export type PacketRenderInterval = Readonly<{
  kind: 'rendered'
  packetIndex: number
  rtpTimestamp: number
  packetSampleOffset: number
  startFrame: number
  endFrame: number
  energy: number
  firstAudibleFrame?: number
}>

export type PacketPlaybackObservation = Readonly<{
  packetIndex: number
  receivedAtMs: number
  decodedAtMs: number
  firstOutputFrame: number
  firstOutputEndFrame: number
  firstOutputAtMs: number
  outputClockContextTime: number
  outputClockPerformanceTime: number
  confirmationObservedAtMs: number
  sampleRate: 48000
  outputClockPassed: true
  sourcePcmOffsetVerified: false
}>

// render callbackは未来のoutputを報告し得る。出力時計が末尾を通過するまで保留する。
export class PacketOutputTracker {
  private pending: PacketRenderInterval[] = []
  private stopped = false
  private nextPacket = 0
  private nextOffset = 0
  private lastEndFrame = -1

  constructor(private readonly confirm: (interval: PacketRenderInterval, atMs: number, clock: {contextTime: number; performanceTime: number; observedAtMs: number}) => void) {}

  record(interval: PacketRenderInterval): void {
    if (this.stopped) return
    if (!Number.isInteger(interval.packetIndex) || interval.packetIndex !== this.nextPacket
      || interval.packetSampleOffset !== this.nextOffset || !Number.isInteger(interval.startFrame)
      || !Number.isInteger(interval.endFrame) || interval.startFrame < 0
      || interval.startFrame < this.lastEndFrame || interval.endFrame <= interval.startFrame
      || interval.packetSampleOffset + interval.endFrame - interval.startFrame > 960
      || !Number.isFinite(interval.energy) || interval.energy < 0) throw new Error('invalid packet render interval')
    if (this.pending.length >= 500) throw new Error('output clock confirmation queue overflow')
    this.pending.push(interval)
    this.nextOffset += interval.endFrame - interval.startFrame
    this.lastEndFrame = interval.endFrame
    if (this.nextOffset === 960) {this.nextOffset = 0; this.nextPacket++}
  }

  poll(timestamp: AudioTimestamp, sampleRate: number): void {
    if (this.stopped || sampleRate !== 48000 || timestamp.contextTime === undefined
      || timestamp.performanceTime === undefined || !Number.isFinite(timestamp.contextTime)
      || !Number.isFinite(timestamp.performanceTime) || timestamp.contextTime <= 0 || timestamp.performanceTime <= 0) return
    const passedFrame = Math.floor(timestamp.contextTime * sampleRate)
    while (this.pending.length && this.pending[0].endFrame <= passedFrame) {
      const interval = this.pending.shift()!
      const atMs = timestamp.performanceTime + (interval.startFrame / sampleRate - timestamp.contextTime) * 1000
      if (!Number.isFinite(atMs) || atMs < 0) throw new Error('invalid packet output clock')
      this.confirm(interval, atMs, {contextTime: timestamp.contextTime, performanceTime: timestamp.performanceTime, observedAtMs: performance.now()})
    }
  }

  stop(): void {this.stopped = true; this.pending = []}
}
