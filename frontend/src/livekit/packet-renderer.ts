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
    // 音切れは送信総数と前後の出力frameから確定する。末尾のゼロを数え続けない。
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

export type SourceAudioFinished = Readonly<{inputSampleCount: number; capturedSampleCount: number; paddingSampleCount: number}>
export type PlaybackCompletion = Readonly<{
  expectedSamples: number; inputSamples: number; paddingSamples: number; renderedSamples: number; packetCount: number
  firstOutputFrame: number; lastOutputEndFrame: number; gapSamples: number; maximumGapSamples: number; gapCount: number
  firstRtpTimestamp: number; lastRtpTimestamp: number
  outputClockContextTime: number; outputClockPerformanceTime: number; confirmationObservedAtMs: number; sampleRate: 48000
}>

export class PacketRenderError extends Error {
  constructor(readonly context: Readonly<Record<string, number>>) {super('invalid packet render interval')}
}

// render callbackは未来のoutputを報告し得る。出力時計が末尾を通過するまで保留する。
export class PacketOutputTracker {
  private pending: PacketRenderInterval[] = []
  private stopped = false
  private nextPacket = 0
  private nextOffset = 0
  private lastEndFrame = -1
  private firstFrame: number | null = null
  private firstRtp: number | null = null
  private lastRtp = 0
  private confirmedSamples = 0
  private confirmedEndFrame = 0
  private gapSamples = 0
  private maximumGapSamples = 0
  private gapCount = 0
  private finished: SourceAudioFinished | null = null
  private completed = false
  private clock: {contextTime: number; performanceTime: number; observedAtMs: number} | null = null

  constructor(private readonly confirm: (interval: PacketRenderInterval, atMs: number, clock: {contextTime: number; performanceTime: number; observedAtMs: number}) => void,
    private readonly complete: (evidence: PlaybackCompletion) => void = () => undefined) {}

  record(interval: PacketRenderInterval): void {
    if (this.stopped) return
    if (!Number.isInteger(interval.packetIndex) || interval.packetIndex !== this.nextPacket
      || interval.packetSampleOffset !== this.nextOffset || !Number.isInteger(interval.startFrame)
      || !Number.isInteger(interval.endFrame) || interval.startFrame < 0
      || interval.startFrame < this.lastEndFrame || interval.endFrame <= interval.startFrame
      || interval.packetSampleOffset + interval.endFrame - interval.startFrame > 960
      || !Number.isFinite(interval.energy) || interval.energy < 0) throw new PacketRenderError({
        packet: interval.packetIndex, expectedPacket: this.nextPacket,
        offset: interval.packetSampleOffset, expectedOffset: this.nextOffset,
        startFrame: interval.startFrame, endFrame: interval.endFrame, previousEndFrame: this.lastEndFrame,
        energy: interval.energy,
      })
    if (!Number.isInteger(interval.rtpTimestamp) || interval.rtpTimestamp < 0 || interval.rtpTimestamp > 0xffffffff) throw new Error('invalid RTP timestamp')
    this.firstRtp ??= interval.rtpTimestamp
    if (((interval.rtpTimestamp - this.firstRtp) >>> 0) !== ((interval.packetIndex * 960) >>> 0)) throw new Error('RTP timeline discontinuity')
    if (this.completed || (this.finished && interval.packetIndex * 960 + interval.packetSampleOffset
      + interval.endFrame - interval.startFrame > this.finished.capturedSampleCount)) throw new Error('render beyond completed source')
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
    this.clock = {contextTime: timestamp.contextTime, performanceTime: timestamp.performanceTime, observedAtMs: performance.now()}
    const passedFrame = Math.floor(timestamp.contextTime * sampleRate)
    while (this.pending.length && this.pending[0].endFrame <= passedFrame) {
      const interval = this.pending.shift()!
      const atMs = timestamp.performanceTime + (interval.startFrame / sampleRate - timestamp.contextTime) * 1000
      if (!Number.isFinite(atMs) || atMs < 0) throw new Error('invalid packet output clock')
      if (this.firstFrame !== null && interval.startFrame > this.confirmedEndFrame) {
        const gap = interval.startFrame - this.confirmedEndFrame
        this.gapSamples += gap; this.maximumGapSamples = Math.max(this.maximumGapSamples, gap); this.gapCount++
      }
      this.firstFrame ??= interval.startFrame
      this.confirmedEndFrame = interval.endFrame
      this.confirmedSamples += interval.endFrame - interval.startFrame
      this.lastRtp = interval.rtpTimestamp
      this.confirm(interval, atMs, this.clock)
    }
    this.completeIfReady()
  }

  finish(source: SourceAudioFinished): void {
    if (this.stopped) return
    if (![source.inputSampleCount, source.capturedSampleCount, source.paddingSampleCount].every(value => Number.isSafeInteger(value) && value >= 0)
      || source.inputSampleCount + source.paddingSampleCount !== source.capturedSampleCount
      || source.capturedSampleCount % 960 !== 0 || source.paddingSampleCount > 1919) throw new Error('invalid source completion')
    if (this.finished && (this.finished.inputSampleCount !== source.inputSampleCount
      || this.finished.capturedSampleCount !== source.capturedSampleCount || this.finished.paddingSampleCount !== source.paddingSampleCount)) throw new Error('conflicting source completion')
    if (this.nextPacket * 960 + this.nextOffset > source.capturedSampleCount) throw new Error('source shorter than rendered packets')
    this.finished = {...source}
    this.completeIfReady()
  }

  private completeIfReady(): void {
    if (this.stopped || this.completed || !this.finished || !this.clock
      || this.confirmedSamples !== this.finished.capturedSampleCount || this.pending.length) return
    this.completed = true
    this.complete({expectedSamples: this.finished.capturedSampleCount, inputSamples: this.finished.inputSampleCount,
      paddingSamples: this.finished.paddingSampleCount, renderedSamples: this.confirmedSamples, packetCount: this.nextPacket,
      firstOutputFrame: this.firstFrame ?? 0, lastOutputEndFrame: this.confirmedEndFrame,
      gapSamples: this.gapSamples, maximumGapSamples: this.maximumGapSamples, gapCount: this.gapCount,
      firstRtpTimestamp: this.firstRtp ?? 0, lastRtpTimestamp: this.lastRtp,
      outputClockContextTime: this.clock.contextTime, outputClockPerformanceTime: this.clock.performanceTime,
      confirmationObservedAtMs: this.clock.observedAtMs, sampleRate: 48000})
  }

  stop(): void {this.stopped = true; this.pending = []}
}
