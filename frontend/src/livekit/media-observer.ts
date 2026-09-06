// encoded frameはdepacketizerの後・decoderの前で、そのまま通過させる。
// workerとwindowのtimeOrigin差を補正し、通知配送の待ち時間を受信時刻へ加えない。
export const encodedObserverWorkerSource = `
self.onrtctransform = (event) => {
  const transformer = event.transformer
  let reported = false
  const observer = new TransformStream({
    transform(frame, controller) {
      const atMs = performance.timeOrigin + performance.now() - transformer.options.windowTimeOrigin
      controller.enqueue(frame)
      if (!reported) {
        reported = true
        self.postMessage({ kind: 'encoded', atMs })
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
  firstEncodedFrameAtMs?: number
  firstDecodedSampleAtMs?: number
  encodedMissingReason?: 'api_unavailable' | 'transform_already_in_use' | 'observer_failed'
  decodedMissingReason?: 'api_unavailable' | 'observer_failed'
}>

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
  private readonly evidence: {
    -readonly [Key in keyof MediaObservation]: MediaObservation[Key]
  }

  constructor(
    private readonly receiver: RTCRtpReceiver | undefined,
    track: MediaStreamTrack,
    private readonly report: (observation: MediaObservation) => void,
  ) {
    this.evidence = { trackReceivedAtMs: performance.now() }
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
      return
    }
    if (this.receiver.transform != null) {
      this.evidence.encodedMissingReason = 'transform_already_in_use'
      return
    }
    const url = URL.createObjectURL(new Blob([encodedObserverWorkerSource], { type: 'text/javascript' }))
    try {
      const worker = new Worker(url)
      this.worker = worker
      worker.onmessage = (event: MessageEvent<{ kind: string; atMs?: number }>) => {
        if (this.closed) return
        if (event.data.kind === 'encoded' && this.evidence.firstEncodedFrameAtMs === undefined
            && typeof event.data.atMs === 'number' && Number.isFinite(event.data.atMs)
            && event.data.atMs >= 0) this.evidence.firstEncodedFrameAtMs = event.data.atMs
        else if (event.data.kind === 'error') { this.failEncoded(); return }
        this.publish()
      }
      worker.onerror = () => this.failEncoded()
      this.transform = new RTCRtpScriptTransform(worker, { windowTimeOrigin: performance.timeOrigin })
      this.receiver.transform = this.transform
    } catch {
      this.worker?.terminate()
      this.worker = null
      this.evidence.encodedMissingReason = 'observer_failed'
    } finally {
      URL.revokeObjectURL(url)
    }
  }

  private failEncoded(): void {
    if (this.closed) return
    this.evidence.encodedMissingReason = 'observer_failed'
    // 観測器の失敗時はpassthroughへ戻し、観測のために再生を停止しない。
    if (this.receiver !== undefined && this.transform !== null
        && this.receiver.transform === this.transform) this.receiver.transform = null
    this.worker?.terminate()
    this.worker = null
    this.transform = null
    this.publish()
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
          let audible = false
          for (let channel = 0; channel < frame.numberOfChannels && !audible; channel += 1) {
            frame.copyTo(samples, { planeIndex: channel, format: 'f32-planar' })
            audible = samples.some((sample) => Number.isFinite(sample) && sample !== 0)
          }
          if (audible) {
            this.evidence.firstDecodedSampleAtMs = availableAt
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
