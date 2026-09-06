import { createHash } from 'node:crypto'
import type { Page } from '@playwright/test'

export type FixtureBoundary = 'sourceStart' | 'speechStart' | 'speechEnd'
export type ClockBounds = { lowerMs: number; upperMs: number; sourceSample: number }
export type ScheduledFixture = {
  samples: number[]
  sampleRate: number
  speechStartSample: number
  speechEndSample: number
  audioSha256: string
}

declare global {
  interface Window {
    __voiceFixtureClock?: {
      start: () => Promise<void>
      close: () => Promise<void>
      bounds: Partial<Record<FixtureBoundary, ClockBounds>>
    }
  }
}

// fixtureのPCMを実際に出すprocess呼び出しを、main→worklet→mainの因果関係で囲む。
// getUserMedia完了、AudioContextの出力デバイス時刻、VADの判定を正解境界に使わない。
export const fixtureWorkletSource = `
class FixtureSource extends AudioWorkletProcessor {
  constructor(options) {
    super()
    const config = options.processorOptions
    this.samples = new Float32Array(config.samples)
    this.boundaries = {sourceStart: 0, speechStart: config.speechStartSample, speechEnd: config.speechEndSample}
    this.reported = new Set()
    this.offset = 0
    this.started = false
    this.lastPing = null
    this.port.onmessage = ({data}) => {
      if (!Number.isFinite(data.sentAtMs) || data.sentAtMs < 0) return
      if (data.type === 'start' && !this.started) this.started = true
      if (data.type === 'start' || data.type === 'ping') this.lastPing = data.sentAtMs
    }
  }
  process(inputs, outputs) {
    const channel = outputs[0][0]
    channel.fill(0)
    if (!this.started || this.lastPing === null) return true
    const end = Math.min(this.offset + channel.length, this.samples.length)
    channel.set(this.samples.subarray(this.offset, end))
    for (const [kind, sample] of Object.entries(this.boundaries)) {
      const inQuantum = kind === 'speechEnd'
        ? this.offset < sample && sample <= this.offset + channel.length
        : this.offset <= sample && sample < this.offset + channel.length
      if (inQuantum && !this.reported.has(kind)) {
        this.reported.add(kind)
        this.port.postMessage({kind, sourceSample: sample, lowerMs: this.lastPing})
      }
    }
    this.offset = end
    return true
  }
}
registerProcessor('voice-quality-fixture', FixtureSource)
`

export const parseScheduledFixture = (
  bytes: Uint8Array,
  metadata: { audio_sha256: string; sample_rate_hz: number; speech_start_sample: number; speech_end_sample: number },
): ScheduledFixture => {
  if (createHash('sha256').update(bytes).digest('hex') !== metadata.audio_sha256) throw new Error('scheduled fixture hash mismatch')
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const name = (start: number, length: number) => new TextDecoder('ascii').decode(bytes.subarray(start, start + length))
  if (bytes.length < 12 || name(0, 4) !== 'RIFF' || name(8, 4) !== 'WAVE') throw new Error('invalid fixture RIFF')
  let sampleRate: number | undefined
  let samples: number[] | undefined
  for (let offset = 12; offset + 8 <= bytes.length;) {
    const size = view.getUint32(offset + 4, true)
    const start = offset + 8
    if (start + size > bytes.length) throw new Error('truncated fixture WAV')
    if (name(offset, 4) === 'fmt ') {
      if (size < 16 || view.getUint16(start, true) !== 1 || view.getUint16(start + 2, true) !== 1
        || view.getUint16(start + 14, true) !== 16) throw new Error('fixture must be mono PCM16')
      sampleRate = view.getUint32(start + 4, true)
    }
    if (name(offset, 4) === 'data') {
      if (size % 2 !== 0) throw new Error('unaligned fixture PCM')
      samples = Array.from({ length: size / 2 }, (_, index) => view.getInt16(start + index * 2, true) / 32768)
    }
    offset = start + size + size % 2
  }
  if (sampleRate !== metadata.sample_rate_hz || !samples || sampleRate === undefined || sampleRate <= 0
    || !Number.isInteger(metadata.speech_start_sample) || !Number.isInteger(metadata.speech_end_sample)
    || metadata.speech_start_sample < 0 || metadata.speech_start_sample >= metadata.speech_end_sample
    || metadata.speech_end_sample > samples.length) throw new Error('invalid fixture sample boundaries')
  return { samples, sampleRate, speechStartSample: metadata.speech_start_sample,
    speechEndSample: metadata.speech_end_sample, audioSha256: metadata.audio_sha256 }
}

export const installScheduledFixture = async (page: Page, fixture: ScheduledFixture): Promise<void> => {
  await page.addInitScript(async ({ fixture, source }) => {
    let context: AudioContext | undefined
    let destination: MediaStreamAudioDestinationNode | undefined
    let worklet: AudioWorkletNode | undefined
    let timer: ReturnType<typeof setInterval> | undefined
    let started = false
    const bounds: Partial<Record<FixtureBoundary, ClockBounds>> = {}
    const nativeGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
    navigator.mediaDevices.getUserMedia = async constraints => {
      if (!constraints?.audio || constraints.video) return nativeGetUserMedia(constraints)
      if (!context) {
        context = new AudioContext({ sampleRate: fixture.sampleRate })
        if (context.sampleRate !== fixture.sampleRate) throw new Error('fixture sample rate changed')
        const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }))
        try { await context.audioWorklet.addModule(url) } finally { URL.revokeObjectURL(url) }
        destination = context.createMediaStreamDestination()
        worklet = new AudioWorkletNode(context, 'voice-quality-fixture', {
          numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1], processorOptions: fixture,
        })
        worklet.port.onmessage = ({ data }) => {
          const upperMs = performance.now()
          if (!['sourceStart', 'speechStart', 'speechEnd'].includes(data.kind)
            || !Number.isFinite(data.lowerMs) || data.lowerMs < 0 || data.lowerMs > upperMs) return
          const kind = data.kind as FixtureBoundary
          bounds[kind] ??= { lowerMs: data.lowerMs, upperMs, sourceSample: data.sourceSample }
          if (kind === 'speechEnd' && timer !== undefined) { clearInterval(timer); timer = undefined }
        }
        worklet.connect(destination)
        await context.resume()
      }
      if (!destination) throw new Error('fixture stream is unavailable')
      return destination.stream.clone()
    }
    window.__voiceFixtureClock = {
      bounds,
      start: async () => {
        if (!context || !worklet || started) throw new Error('fixture is not ready or already started')
        await context.resume()
        started = true
        worklet.port.postMessage({ type: 'start', sentAtMs: performance.now() })
        timer = setInterval(() => worklet?.port.postMessage({ type: 'ping', sentAtMs: performance.now() }), 2)
      },
      close: async () => {
        if (timer !== undefined) clearInterval(timer)
        worklet?.disconnect()
        for (const track of destination?.stream.getTracks() ?? []) track.stop()
        await context?.close()
      },
    }
  }, { fixture, source: fixtureWorkletSource })
}

export const readFixtureBounds = async (page: Page, maximumUncertaintyMs = 20) => {
  const bounds = await page.evaluate(() => window.__voiceFixtureClock?.bounds)
  for (const kind of ['sourceStart', 'speechStart', 'speechEnd'] as const) {
    const value = bounds?.[kind]
    if (!value || !Number.isFinite(value.lowerMs) || !Number.isFinite(value.upperMs)
      || value.lowerMs < 0 || value.upperMs < value.lowerMs || value.upperMs - value.lowerMs > maximumUncertaintyMs) {
      throw new Error(`fixture clock boundary is unavailable or uncertain: ${kind}`)
    }
  }
  return bounds as Record<FixtureBoundary, ClockBounds>
}
