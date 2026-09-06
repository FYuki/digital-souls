import { WorkerClockCalibration, type ClockBounds } from './worker-clock'

// encoded frameは変更せず通す。workerの生monotonic時刻だけを通知し、main側で較正する。
export const encodedObserverWorkerSource = `
self.onmessage = (event) => {
  if (event.data.kind === 'clock' && Number.isInteger(event.data.sequence)) {
    self.postMessage({ kind: 'clock', sequence: event.data.sequence, workerAtMs: performance.now() })
  }
}
self.onrtctransform = (event) => {
  const transformer = event.transformer
  let reported = false
  const observer = new TransformStream({
    transform(frame, controller) {
      const workerAtMs = performance.now()
      let packet
      if (!reported) {
        try {
          const metadata = frame.getMetadata()
          const receivedAtWorkerMs = metadata.receiveTime
          const u32 = value => Number.isInteger(value) && value >= 0 && value <= 0xffffffff
          if (u32(metadata.rtpTimestamp) && u32(metadata.synchronizationSource)
            && Number.isFinite(receivedAtWorkerMs) && receivedAtWorkerMs >= 0) {
            packet = { rtpTimestamp: metadata.rtpTimestamp, source: metadata.synchronizationSource, receivedAtWorkerMs }
          }
        } catch { /* 観測APIの失敗で音声frameを破棄しない。 */ }
      }
      controller.enqueue(frame)
      if (!reported) {
        reported = true
        self.postMessage({ kind: 'encoded', workerAtMs, ...(packet ? { packet } : {}) })
      }
    },
  })
  transformer.readable.pipeThrough(observer).pipeTo(transformer.writable).catch(() => {
    self.postMessage({ kind: 'error' })
  })
}
`

export type MediaObservation = Readonly<{
  trackReceivedAtMs: number
  // scalarは上下限の下限。packet受信→配送の差分を過小評価しない。
  firstEncodedFrameAtMs?: number
  firstEncodedFrameAtBoundsMs?: ClockBounds
  firstPacketReceivedAtBoundsMs?: ClockBounds
  workerClockOffsetBoundsMs?: ClockBounds
  workerClockMethod?: 'causal_message_bounds'
  // comfort noiseも含む。RTP packetやresponseに相関済みのdecode時刻ではない。
  firstNonzeroDecodedFrameAtMs?: number
  firstPacketReceivedAtMs?: number
  firstPacketDeliveredAtMs?: number
  packetDeliveryMissingReason?: 'synchronization_api_unavailable' | 'packet_metadata_unavailable'
    | 'matching_packet_not_observed' | 'packet_clock_invalid' | 'observer_failed' | 'clock_calibration_failed'
  encodedMissingReason?: 'api_unavailable' | 'transform_already_in_use' | 'observer_failed' | 'clock_calibration_failed'
  decodedMissingReason?: 'api_unavailable' | 'observer_failed'
}>

type RawEncodedPacket = { rtpTimestamp: number; source: number; receivedAtWorkerMs: number }
type WorkerObservation = { kind: string; sequence?: number; workerAtMs?: number; packet?: RawEncodedPacket }
type EncodedPacket = { rtpTimestamp: number; source: number; receivedAtMs: number }
const validU32 = (value: number) => Number.isInteger(value) && value >= 0 && value <= 0xffffffff
const packetKey = (packet: Pick<EncodedPacket, 'source' | 'rtpTimestamp'>) => `${packet.source}:${packet.rtpTimestamp}`

type ProcessorConstructor = new (options: { track: MediaStreamTrack }) => {
  readable: ReadableStream<AudioData>
}

// track単位の最初のmediaだけを記録する。後続responseへの帰属を時刻の推測で付けない。
export class RemoteMediaObserver {
  private worker: Worker | null = null
  private transform: RTCRtpScriptTransform | null = null
  private reader: ReadableStreamDefaultReader<AudioData> | null = null
  private clone: MediaStreamTrack | null = null
  private closed = false
  private packet: EncodedPacket | null = null
  private readonly clock = new WorkerClockCalibration()
  private clockSamples = 0
  private clockRequest: { sequence: number; sentAtMs: number } | null = null
  private clockTimeout: ReturnType<typeof setTimeout> | null = null
  private encoded: WorkerObservation | null = null
  private readonly deliveredPackets = new Map<string, number>()
  private synchronizationTimer: ReturnType<typeof setInterval> | null = null
  private synchronizationTimeout: ReturnType<typeof setTimeout> | null = null
  private readonly evidence: {
    -readonly [Key in keyof MediaObservation]: MediaObservation[Key]
  }

  constructor(
    private readonly receiver: RTCRtpReceiver | undefined,
    track: MediaStreamTrack,
    private readonly report: (observation: MediaObservation) => void,
  ) {
    this.evidence = { trackReceivedAtMs: performance.now() }
    this.observePacketDelivery()
    this.observeEncoded()
    this.observeDecoded(track)
    this.publish()
  }

  snapshot(): MediaObservation {
    return { ...this.evidence }
  }

  close(): void {
    if (this.closed) return
    this.closed = true
    this.stopClockCalibration()
    this.stopPacketDelivery()
    if (this.receiver !== undefined && this.transform !== null
        && this.receiver.transform === this.transform) this.receiver.transform = null
    this.worker?.terminate()
    this.worker = null
    this.transform = null
    this.closeDecodedReader()
  }

  private observeEncoded(): void {
    if (this.receiver === undefined || typeof RTCRtpScriptTransform === 'undefined'
        || typeof Worker === 'undefined') {
      this.evidence.encodedMissingReason = 'api_unavailable'
      this.evidence.packetDeliveryMissingReason ??= 'packet_metadata_unavailable'
      this.stopPacketDelivery()
      return
    }
    if (this.receiver.transform != null) {
      this.evidence.encodedMissingReason = 'transform_already_in_use'
      this.evidence.packetDeliveryMissingReason ??= 'packet_metadata_unavailable'
      this.stopPacketDelivery()
      return
    }
    const url = URL.createObjectURL(new Blob([encodedObserverWorkerSource], { type: 'text/javascript' }))
    try {
      const worker = new Worker(url)
      this.worker = worker
      worker.onmessage = (event: MessageEvent<WorkerObservation>) => {
        if (this.closed || this.worker !== worker) return
        if (event.data.kind === 'clock') {
          const request = this.clockRequest
          if (request === null || event.data.sequence !== request.sequence) return
          this.clockRequest = null
          if (typeof event.data.workerAtMs !== 'number') { this.failEncoded('clock_calibration_failed'); return }
          this.clock.add(request.sentAtMs, event.data.workerAtMs, performance.now())
          this.clockSamples += 1
          if (this.clockSamples < 10) this.requestClockSample()
          else {
            this.stopClockCalibration()
            const bounds = this.clock.bounds()
            if (bounds === undefined) { this.failEncoded('clock_calibration_failed'); return }
            this.evidence.workerClockMethod = 'causal_message_bounds'
            this.evidence.workerClockOffsetBoundsMs = bounds
            this.applyEncodedObservation()
          }
        } else if (event.data.kind === 'encoded' && this.encoded === null) {
          this.encoded = event.data
          this.applyEncodedObservation()
        } else if (event.data.kind === 'error') { this.failEncoded(); return }
        this.publish()
      }
      worker.onerror = () => this.failEncoded()
      this.transform = new RTCRtpScriptTransform(worker)
      this.receiver.transform = this.transform
      this.clockTimeout = setTimeout(() => this.failEncoded('clock_calibration_failed'), 1000)
      this.requestClockSample()
    } catch {
      this.failEncoded()
    } finally {
      URL.revokeObjectURL(url)
    }
  }

  private requestClockSample(): void {
    this.clockRequest = { sequence: this.clockSamples, sentAtMs: performance.now() }
    this.worker?.postMessage({ kind: 'clock', sequence: this.clockSamples })
  }

  private stopClockCalibration(): void {
    if (this.clockTimeout !== null) clearTimeout(this.clockTimeout)
    this.clockTimeout = null
    this.clockRequest = null
  }

  private applyEncodedObservation(): void {
    if (this.encoded === null || this.clock.bounds() === undefined
      || this.evidence.firstEncodedFrameAtMs !== undefined) return
    const encodedAt = this.clock.toMain(this.encoded.workerAtMs ?? NaN)
    if (encodedAt === undefined || encodedAt.lowerMs > performance.now()) {
      this.failEncoded('clock_calibration_failed')
      return
    }
    this.evidence.firstEncodedFrameAtBoundsMs = encodedAt
    this.evidence.firstEncodedFrameAtMs = encodedAt.lowerMs
    const packet = this.encoded.packet
    if (!packet || !validU32(packet.source) || !validU32(packet.rtpTimestamp)) {
      this.evidence.packetDeliveryMissingReason ??= 'packet_metadata_unavailable'
      this.stopPacketDelivery()
      return
    }
    const receivedAt = this.clock.toMain(packet.receivedAtWorkerMs)
    if (receivedAt === undefined || receivedAt.lowerMs > encodedAt.upperMs) {
      this.evidence.packetDeliveryMissingReason = 'packet_clock_invalid'
      this.stopPacketDelivery()
      return
    }
    this.packet = { source: packet.source, rtpTimestamp: packet.rtpTimestamp, receivedAtMs: receivedAt.lowerMs }
    this.evidence.firstPacketReceivedAtBoundsMs = receivedAt
    this.evidence.firstPacketReceivedAtMs = receivedAt.lowerMs
    // publish後に長く無音だった場合、最初のpacketが来てから観測窓を開き直す。
    if (this.evidence.packetDeliveryMissingReason === 'matching_packet_not_observed') {
      delete this.evidence.packetDeliveryMissingReason
      this.observePacketDelivery()
    }
    this.matchPacketDelivery()
  }

  private failEncoded(reason: 'observer_failed' | 'clock_calibration_failed' = 'observer_failed'): void {
    if (this.closed) return
    this.stopClockCalibration()
    this.evidence.encodedMissingReason = reason
    this.evidence.packetDeliveryMissingReason ??= reason
    this.stopPacketDelivery()
    // 観測器の失敗時はpassthroughへ戻し、観測のために再生を停止しない。
    if (this.receiver !== undefined && this.transform !== null
        && this.receiver.transform === this.transform) this.receiver.transform = null
    this.worker?.terminate()
    this.worker = null
    this.transform = null
    this.publish()
  }

  private observePacketDelivery(): void {
    if (typeof this.receiver?.getSynchronizationSources !== 'function') {
      this.evidence.packetDeliveryMissingReason = 'synchronization_api_unavailable'
      return
    }
    const poll = () => {
      try {
        for (const source of this.receiver!.getSynchronizationSources()) {
          if (!validU32(source.source) || !validU32(source.rtpTimestamp)
            || !Number.isFinite(source.timestamp)) continue
          // 仕様の配送時刻はepoch基準。AudioData.timestampを配送時刻に流用しない。
          const deliveredAtMs = source.timestamp - performance.timeOrigin
          const key = packetKey(source)
          if (!this.deliveredPackets.has(key)) {
            this.deliveredPackets.set(key, deliveredAtMs)
            if (this.deliveredPackets.size > 128) this.deliveredPackets.delete(this.deliveredPackets.keys().next().value!)
          }
        }
        this.matchPacketDelivery()
      } catch {
        this.evidence.packetDeliveryMissingReason = 'observer_failed'
        this.stopPacketDelivery()
        this.publish()
      }
    }
    this.synchronizationTimer = setInterval(poll, 2)
    this.synchronizationTimeout = setTimeout(() => {
      this.evidence.packetDeliveryMissingReason = 'matching_packet_not_observed'
      this.stopPacketDelivery()
      this.publish()
    }, 2000)
    poll()
  }

  private matchPacketDelivery(): void {
    if (this.packet === null || this.evidence.firstPacketDeliveredAtMs !== undefined
      || this.evidence.packetDeliveryMissingReason !== undefined) return
    const deliveredAtMs = this.deliveredPackets.get(packetKey(this.packet))
    if (deliveredAtMs === undefined) return
    if (!Number.isFinite(deliveredAtMs) || deliveredAtMs < this.packet.receivedAtMs
      || deliveredAtMs > performance.now()) {
      this.evidence.packetDeliveryMissingReason = 'packet_clock_invalid'
    } else {
      this.evidence.firstPacketDeliveredAtMs = deliveredAtMs
    }
    this.stopPacketDelivery()
    this.publish()
  }

  private stopPacketDelivery(): void {
    if (this.synchronizationTimer !== null) clearInterval(this.synchronizationTimer)
    if (this.synchronizationTimeout !== null) clearTimeout(this.synchronizationTimeout)
    this.synchronizationTimer = null
    this.synchronizationTimeout = null
    this.deliveredPackets.clear()
  }

  private observeDecoded(track: MediaStreamTrack): void {
    const Processor = (globalThis as typeof globalThis & {
      MediaStreamTrackProcessor?: ProcessorConstructor
    }).MediaStreamTrackProcessor
    if (Processor === undefined) {
      this.evidence.decodedMissingReason = 'api_unavailable'
      return
    }
    try {
      this.clone = track.clone()
      const reader = new Processor({ track: this.clone }).readable.getReader()
      this.reader = reader
      void this.readDecoded(reader)
    } catch {
      this.evidence.decodedMissingReason = 'observer_failed'
      this.closeDecodedReader()
    }
  }

  private async readDecoded(reader: ReadableStreamDefaultReader<AudioData>): Promise<void> {
    try {
      while (!this.closed) {
        const { value: frame, done } = await reader.read()
        if (done) break
        try {
          if (this.closed) break
          // AudioData.timestampはcapture clockであり、decode完了時刻として使わない。
          const availableAt = performance.now()
          const samples = new Float32Array(frame.numberOfFrames)
          let hasNonzeroSamples = false
          for (let channel = 0; channel < frame.numberOfChannels && !hasNonzeroSamples; channel += 1) {
            frame.copyTo(samples, { planeIndex: channel, format: 'f32-planar' })
            hasNonzeroSamples = samples.some((sample) => Number.isFinite(sample) && sample !== 0)
          }
          if (hasNonzeroSamples) {
            this.evidence.firstNonzeroDecodedFrameAtMs = availableAt
            this.publish()
            break
          }
        } finally {
          frame.close()
        }
      }
    } catch {
      if (!this.closed) {
        this.evidence.decodedMissingReason = 'observer_failed'
        this.publish()
      }
    } finally {
      this.closeDecodedReader()
    }
  }

  private closeDecodedReader(): void {
    const reader = this.reader
    this.reader = null
    void reader?.cancel().catch(() => undefined)
    this.clone?.stop()
    this.clone = null
  }

  private publish(): void {
    if (!this.closed) this.report(this.snapshot())
  }
}
