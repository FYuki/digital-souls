// Sileroの音声確率で発話を確認し、PCMの活動区間から無音長を測る。
// 確率の余韻を発話末尾と見なさず、無音600ms以内の文の続きを保持する。
export type UtteranceDetection = Readonly<{
  type: 'candidate' | 'confirmed' | 'ended' | 'misfire'
  speechStartedAtMs: number
  detectedAtMs: number
}>

export type UtteranceDetectorOptions = Readonly<{
  sampleRate: number
  minimumRms: number
  minimumActiveMs: number
  silenceMs: number
  strongSpeechProbability: number
  minimumStrongMs: number
  negativeSpeechProbability: number
  neuralSilenceMs: number
  evidenceWindowMs: number
}>

export const utteranceDetectorOptions: UtteranceDetectorOptions = {
  sampleRate: 16_000,
  minimumRms: 0.001,
  minimumActiveMs: 100,
  silenceMs: 600,
  strongSpeechProbability: 0.3,
  minimumStrongMs: 300,
  negativeSpeechProbability: 0.25,
  neuralSilenceMs: 700,
  evidenceWindowMs: 2000,
}

export class UtteranceDetector {
  private candidateStart: number | null = null
  private lastActiveEnd = 0
  private activeMs = 0
  private strongFrames: { end: number; duration: number }[] = []
  private neuralSilenceMs = 0
  private confirmed = false

  constructor(
    private readonly emit: (event: UtteranceDetection) => void,
    private readonly options: UtteranceDetectorOptions = utteranceDetectorOptions,
  ) {}

  reset(): void {
    this.candidateStart = null
    this.lastActiveEnd = 0
    this.activeMs = 0
    this.strongFrames = []
    this.neuralSilenceMs = 0
    this.confirmed = false
  }

  process(frame: Float32Array, speechProbability: number, frameEndMs: number): void {
    if (frame.length === 0 || !Number.isFinite(frameEndMs) || frameEndMs < 0
      || !Number.isFinite(speechProbability) || speechProbability < 0 || speechProbability > 1) return
    const durationMs = frame.length * 1000 / this.options.sampleRate
    let energy = 0
    for (const sample of frame) {
      if (!Number.isFinite(sample)) return
      energy += sample * sample
    }
    const active = Math.sqrt(energy / frame.length) >= this.options.minimumRms
    if (active) {
      if (this.candidateStart === null) {
        this.candidateStart = Math.max(0, frameEndMs - durationMs)
        this.emit({ type: 'candidate', speechStartedAtMs: this.candidateStart, detectedAtMs: frameEndMs })
      }
      this.lastActiveEnd = frameEndMs
      this.activeMs += durationMs
    }
    if (this.candidateStart === null) return
    // 背景音がPCM閾値を超え続けても、離れた確率ピークを無期限に合算しない。
    const evidenceStart = frameEndMs - this.options.evidenceWindowMs
    this.strongFrames = this.strongFrames.filter(item => item.end > evidenceStart)
    if (speechProbability >= this.options.strongSpeechProbability) {
      this.strongFrames.push({ end: frameEndMs, duration: durationMs })
      this.neuralSilenceMs = 0
    } else if (speechProbability < this.options.negativeSpeechProbability) {
      this.neuralSilenceMs += durationMs
    }
    const strongMs = this.strongFrames.reduce((total, item) => (
      total + Math.min(item.duration, item.end - evidenceStart)
    ), 0)
    // PCMに環境音が残る場合は従来の確率による無音判定でも終了できる。
    // 中間確率ではカウンターを進めず、正の確率で解除するhysteresisを維持する。
    const ended = frameEndMs - this.lastActiveEnd > this.options.silenceMs
      || (this.confirmed && this.neuralSilenceMs > this.options.neuralSilenceMs)
    // 短い単発の確率上昇を発話へ昇格させない。現行legacyの4 frame確認を保つ。
    if (!this.confirmed && this.activeMs >= this.options.minimumActiveMs
      && strongMs >= this.options.minimumStrongMs) {
      this.confirmed = true
      this.emit({ type: 'confirmed', speechStartedAtMs: this.candidateStart, detectedAtMs: frameEndMs })
    }
    if (ended) {
      const event: UtteranceDetection = {
        type: this.confirmed ? 'ended' : 'misfire',
        speechStartedAtMs: this.candidateStart,
        detectedAtMs: frameEndMs,
      }
      this.reset()
      this.emit(event)
    }
  }
}
