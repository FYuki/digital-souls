import {RemoteMediaObserver, type DecodedAudioPacket} from './media-observer'
import {PacketOutputDiagnostic, type PacketOutputEvidence} from './packet-output-diagnostic'
import {PacketOutputTracker, packetRendererSource, type PacketRenderInterval,
  type PlaybackCompletion, type SourceAudioFinished} from './packet-renderer'
import {RtpPacketSequence} from './rtp-packet-sequence'

export const AUDIO_PROBE_TRACK_PREFIX = 'ds-audio-probe-v1:'
type ProbeFailure = 'unavailable' | 'busy' | 'timeout' | 'connection_changed' | 'track_unsubscribed'
  | 'send_failed' | 'audio_graph' | 'media_decoder' | 'rtp_timeline' | 'renderer' | 'output_clock'
  | 'packet_evidence' | 'cleanup_failed'
export type AudioProbeObservation = Readonly<{
  scope: 'rtc_audio_probe'; probeId: string; generation: number; requestedAtMs: number
  trackSid: string | null; status: 'captured' | 'failed'; reason?: ProbeFailure
  completedAtMs: number; completion: PlaybackCompletion | null
  packetOutputs: ReadonlyArray<PacketOutputEvidence>; cleanupCompleted: boolean
}>
type ProbeTrack = Readonly<{receiver?: RTCRtpReceiver; mediaStreamTrack: MediaStreamTrack}>
type ProbeFrame = Record<string, string | number>

// 診断専用trackにも会話と同じpacket復号・renderer・出力時計を使う。Coreの応答には帰属させない。
export class AudioAvailabilityProbe {
  readonly requestedAtMs = performance.now()
  readonly result: Promise<AudioProbeObservation>
  private resolve!: (result: AudioProbeObservation) => void
  private settling = false
  private failure: ProbeFailure | undefined
  private trackSid: string | null = null
  private publisherSid: string | null = null
  private context: AudioContext | null = null
  private observer: RemoteMediaObserver | null = null
  private worklet: AudioWorkletNode | null = null
  private gain: GainNode | null = null
  private element: HTMLAudioElement | null = null
  private tracker: PacketOutputTracker | null = null
  private diagnostic: PacketOutputDiagnostic | null = null
  private readonly sequence = new RtpPacketSequence()
  private readonly rows: PacketOutputEvidence[] = []
  private outputTimer: ReturnType<typeof setInterval> | null = null
  private readonly deadline: ReturnType<typeof setTimeout>

  constructor(readonly probeId: string, readonly generation: number,
    private readonly current: () => boolean, private readonly send: (frame: ProbeFrame) => Promise<void>) {
    this.result = new Promise(resolve => {this.resolve = resolve})
    this.deadline = setTimeout(() => this.cancel('timeout'), 4000)
  }

  start(): void {
    if (!this.active()) return
    void this.send(this.frame('audio_probe_request')).catch(() => this.cancel('send_failed'))
  }

  matchesTrack(name: string): boolean {return name === AUDIO_PROBE_TRACK_PREFIX + this.probeId}

  attach(track: ProbeTrack, trackSid: string, publisherSid: string): void {
    if (!this.active() || this.trackSid !== null || !/^TR_[A-Za-z0-9_-]{1,100}$/.test(trackSid) || !publisherSid) return
    this.trackSid = trackSid
    this.publisherSid = publisherSid
    void this.setup(track).catch(() => this.cancel('audio_graph'))
  }

  finish(probeId: string, generation: number, trackSid: string, publisherSid: string, source: SourceAudioFinished): void {
    if (!this.active() || probeId !== this.probeId || generation !== this.generation
      || trackSid !== this.trackSid || publisherSid !== this.publisherSid) return
    try {
      if (!this.tracker || source.inputSampleCount !== 9600 || source.capturedSampleCount !== 10560
        || source.paddingSampleCount !== 960) throw new Error('invalid probe source')
      this.tracker.finish(source)
    } catch {this.cancel('packet_evidence')}
  }

  unsubscribe(trackSid: string): void {if (!this.settling && trackSid === this.trackSid) this.cancel('track_unsubscribed')}

  cancel(reason: ProbeFailure = 'connection_changed'): void {
    // 完了ACKの途中でも世代変更を成功扱いしない。最初の具体的な失敗理由を保持する。
    this.failure ??= reason
    if (!this.settling) void this.settle(null)
  }

  private active(): boolean {
    if (this.settling) return false
    if (!this.current()) {this.cancel('connection_changed'); return false}
    return true
  }

  private frame(type: string): ProbeFrame {
    return {protocol_version: '1.0', type, probe_id: this.probeId, generation: this.generation,
      ...(type === 'audio_probe_request' || this.trackSid === null ? {} : {track_sid: this.trackSid})}
  }

  private async setup(track: ProbeTrack): Promise<void> {
    const context = new AudioContext({sampleRate: 48000})
    this.context = context
    const observer = new RemoteMediaObserver(track.receiver, track.mediaStreamTrack, () => undefined, {
      packet: packet => this.receive(packet), failed: () => this.cancel('media_decoder'),
      interrupted: () => this.cancel('rtp_timeline'),
    })
    this.observer = observer
    if (!this.active()) {observer.close(); return}
    const url = URL.createObjectURL(new Blob([packetRendererSource], {type: 'text/javascript'}))
    try {await Promise.all([context.resume(), context.audioWorklet.addModule(url), observer.ready()])}
    finally {URL.revokeObjectURL(url)}
    if (!this.active()) return
    const worklet = new AudioWorkletNode(context, 'packet-renderer', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
    })
    this.worklet = worklet
    const gain = context.createGain()
    this.gain = gain
    gain.gain.value = 1
    worklet.connect(gain)
    gain.connect(context.destination)
    this.diagnostic = new PacketOutputDiagnostic(this.probeId, this.trackSid!, this.generation, row => {
      if (this.rows.length >= 200) {this.cancel('packet_evidence'); return}
      this.rows.push(row)
      if (row.status !== 'captured') this.cancel('packet_evidence')
    })
    const tracker = new PacketOutputTracker((interval, atMs, clock) => {
      if (this.active()) this.diagnostic?.confirm(interval, atMs, clock.observedAtMs)
    }, completion => {
      if (!this.active()) return
      if (completion.renderedSamples !== 10560 || completion.gapSamples !== 0
        || !this.rows.some(row => row.status === 'captured' && row.firstAudibleAtMs !== undefined)) {
        this.cancel('packet_evidence'); return
      }
      void this.settle(completion)
    })
    this.tracker = tracker
    worklet.port.onmessage = (event: MessageEvent<PacketRenderInterval | {kind: 'error'}>) => {
      if (!this.active()) return
      try {
        if (event.data.kind === 'rendered') tracker.record(event.data)
        else if (event.data.kind === 'error') this.cancel('renderer')
      } catch {this.cancel('renderer')}
    }
    this.outputTimer = setInterval(() => {
      if (!this.active()) return
      try {tracker.poll(context.getOutputTimestamp(), context.sampleRate)}
      catch {this.cancel('output_clock')}
    }, 5)
    const element = document.createElement('audio')
    this.element = element
    element.autoplay = true
    element.hidden = true
    element.muted = true
    element.srcObject = new MediaStream([track.mediaStreamTrack])
    document.body.append(element)
    await this.send(this.frame('audio_probe_ready'))
  }

  private receive(packet: DecodedAudioPacket): void {
    if (!this.active()) return
    try {
      if (!this.worklet || !this.diagnostic || packet.packetIndex >= 11
        || packet.receivedAtBoundsMs.lowerMs < this.requestedAtMs
        || packet.receivedAtMs > packet.decodedAtMs) {this.cancel('packet_evidence'); return}
      if (this.sequence.receive(packet)) {this.cancel('rtp_timeline'); return}
      this.diagnostic.receive(packet)
      this.worklet.port.postMessage({kind: 'pcm', packetIndex: packet.packetIndex,
        rtpTimestamp: packet.rtpTimestamp, samples: packet.pcm}, [packet.pcm.buffer])
    } catch {this.cancel('rtp_timeline')}
  }

  private async settle(completion: PlaybackCompletion | null): Promise<void> {
    if (this.settling) return
    this.settling = true
    clearTimeout(this.deadline)
    if (this.outputTimer !== null) clearInterval(this.outputTimer)
    this.tracker?.stop()
    this.observer?.close()
    this.worklet?.port.postMessage({kind: 'stop'})
    this.worklet?.disconnect()
    this.worklet?.port.close()
    if (this.gain) {this.gain.gain.value = 0; this.gain.disconnect()}
    if (this.element) {this.element.srcObject = null; this.element.remove()}
    let cleanupCompleted = true
    let closeDeadline: ReturnType<typeof setTimeout> | undefined
    try {
      // 終了処理の失敗も記録する。Browserの診断Promiseを無期限に残さない。
      await Promise.race([this.context?.close(), new Promise<never>((_, reject) => {
        closeDeadline = setTimeout(() => reject(new Error('cleanup timeout')), 500)
      })])
    } catch {cleanupCompleted = false; this.failure ??= 'cleanup_failed'}
    finally {clearTimeout(closeDeadline)}
    if (!this.current()) this.failure ??= 'connection_changed'
    if (completion && !this.failure) {
      let sendDeadline: ReturnType<typeof setTimeout> | undefined
      try {
        await Promise.race([this.send(this.frame('audio_probe_complete')), new Promise<never>((_, reject) => {
          sendDeadline = setTimeout(() => reject(new Error('send timeout')), 500)
        })])
      } catch {this.failure ??= 'send_failed'}
      finally {clearTimeout(sendDeadline)}
    }
    if (!this.current()) this.failure ??= 'connection_changed'
    this.resolve({scope: 'rtc_audio_probe', probeId: this.probeId, generation: this.generation,
      requestedAtMs: this.requestedAtMs, trackSid: this.trackSid, completedAtMs: performance.now(),
      status: this.failure ? 'failed' : 'captured', ...(this.failure ? {reason: this.failure} : {}),
      completion, packetOutputs: [...this.rows], cleanupCompleted})
  }
}
