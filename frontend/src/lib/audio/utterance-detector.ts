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
  shortStrongMs: number
  highSpeechProbability: number
  minimumHighMs: number
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
  shortStrongMs: 250,
  highSpeechProbability: 0.5,
  minimumHighMs: 192,
  negativeSpeechProbability: 0.25,
  neuralSilenceMs: 700,
  evidenceWindowMs: 2000,
}

export class UtteranceDetector {
  private candidateStart: number | null = null
  private lastActiveEnd = 0
  private activeFrames: { start: number; end: number }[] = []
  private strongFrames: { end: number; duration: number }[] = []
  private neuralSilenceMs = 0
  private confirmed = false
  private consecutiveHighMs = 0

  constructor(
    private readonly emit: (event: UtteranceDetection) => void,
    private readonly options: UtteranceDetectorOptions = utteranceDetectorOptions,
  ) {}

  reset(): void {
    this.candidateStart = null
    this.lastActiveEnd = 0
    this.activeFrames = []
    this.strongFrames = []
    this.neuralSilenceMs = 0
    this.confirmed = false
    this.consecutiveHighMs = 0
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
    const evidenceStart = frameEndMs - this.options.evidenceWindowMs
    // 確定前のPCM活動も確率と同じ窓に限定する。古い背景音の開始時刻を
    // 後の発話へ引き継ぐと、サーバーのpre-rollに存在しない音声を指してしまう。
    if (!this.confirmed) {
      this.activeFrames = this.activeFrames.filter(item => item.end > evidenceStart)
    }
    if (active) {
      if (this.candidateStart === null) {
        this.candidateStart = Math.max(0, frameEndMs - durationMs)
        this.emit({ type: 'candidate', speechStartedAtMs: this.candidateStart, detectedAtMs: frameEndMs })
      }
      this.lastActiveEnd = frameEndMs
      if (!this.confirmed) {
        this.activeFrames.push({ start: Math.max(0, frameEndMs - durationMs), end: frameEndMs })
      }
    }
    if (this.candidateStart === null) return
    // 背景音がPCM閾値を超え続けても、離れた確率ピークを無期限に合算しない。
    if (!this.confirmed && this.activeFrames.length > 0) {
      this.candidateStart = Math.max(this.activeFrames[0].start, evidenceStart)
    }
    this.strongFrames = this.strongFrames.filter(item => item.end > evidenceStart)
    if (speechProbability >= this.options.strongSpeechProbability) {
      this.strongFrames.push({ end: frameEndMs, duration: durationMs })
      this.neuralSilenceMs = 0
    } else if (speechProbability < this.options.negativeSpeechProbability) {
      this.neuralSilenceMs += durationMs
    }
    this.consecutiveHighMs = speechProbability >= this.options.highSpeechProbability
      ? Math.min(this.consecutiveHighMs + durationMs, this.options.minimumHighMs) : 0
    const strongMs = this.strongFrames.reduce((total, item) => (
      total + Math.min(item.duration, item.end - evidenceStart)
    ), 0)
    // PCMに環境音が残る場合は従来の確率による無音判定でも終了できる。
    // 中間確率ではカウンターを進めず、正の確率で解除するhysteresisを維持する。
    const ended = frameEndMs - this.lastActiveEnd > this.options.silenceMs
      || (this.confirmed && this.neuralSilenceMs > this.options.neuralSilenceMs)
    // 通常はlegacyの4 frame相当を確認する。短い発話は3 frame相当の根拠と
    // 直近2 frameの連続した高確率が揃った場合だけ確定し、散在する弱い山を合算しない。
    const activeMs = this.activeFrames.reduce((total, item) => (
      total + item.end - Math.max(item.start, evidenceStart)
    ), 0)
    if (!this.confirmed && activeMs >= this.options.minimumActiveMs
      && (strongMs >= this.options.minimumStrongMs
        || (strongMs >= this.options.shortStrongMs && this.consecutiveHighMs >= this.options.minimumHighMs))) {
      this.confirmed = true
      this.activeFrames = []
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
