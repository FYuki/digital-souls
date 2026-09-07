import type {ShortSpeechEvidence} from './short-speech-evidence'

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
  evidenceProbability: number
  minimumEvidenceMs: number
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
  highSpeechProbability: 0.4,
  minimumHighMs: 192,
  evidenceProbability: 0.2,
  minimumEvidenceMs: 140,
  negativeSpeechProbability: 0.25,
  neuralSilenceMs: 700,
  evidenceWindowMs: 2000,
}

export const shortSpeechFallbackOptions = {
  maximumActiveSpanMs: 1000,
  minimumVoicedEvidenceMs: 160,
  minimumVoicedFraction: 0.6,
  maximumTonalConcentration: 0.9,
  maximumSpectralFlatness: 0.3,
} as const

export class UtteranceDetector {
  private pendingVoiceFrames: {end: number; duration: number}[] = []
  private shortSpeechFrames: {end: number; duration: number}[] = []
  private candidateStart: number | null = null
  private lastActiveEnd = 0
  private activeFrames: { start: number; end: number }[] = []
  private evidenceFrames: {end: number; duration: number; probability: number}[] = []
  private strongFrames: { end: number; duration: number }[] = []
  private neuralSilenceMs = 0
  private confirmed = false
  private consecutiveHighMs = 0

  constructor(
    private readonly emit: (event: UtteranceDetection) => void,
    private readonly options: UtteranceDetectorOptions = utteranceDetectorOptions,
  ) {}

  reset(): void {
    this.pendingVoiceFrames = []
    this.shortSpeechFrames = []
    this.candidateStart = null
    this.lastActiveEnd = 0
    this.activeFrames = []
    this.strongFrames = []
    this.evidenceFrames = []
    this.neuralSilenceMs = 0
    this.confirmed = false
    this.consecutiveHighMs = 0
  }

  process(frame: Float32Array, speechProbability: number, frameEndMs: number, secondary?: ShortSpeechEvidence): void {
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
    this.shortSpeechFrames = this.shortSpeechFrames.filter(item => item.end > evidenceStart)
    this.pendingVoiceFrames = this.pendingVoiceFrames.filter(item => item.end > evidenceStart)
    // 不正な補助値は無視し、主VADの通常判定は続ける。
    const validSecondary = secondary !== undefined
      && [secondary.voicedFraction, secondary.tonalConcentration, secondary.spectralFlatness]
        .every(value => Number.isFinite(value) && value >= 0 && value <= 1)
    // 有声の短い母音は周波数が集中する場合がある。確定には従来の雑音除外を
    // 要求し、ここでは後続語を待つ根拠だけを記録する。
    if (!this.confirmed && active && validSecondary && secondary
      && secondary.voicedFraction >= shortSpeechFallbackOptions.minimumVoicedFraction
      && secondary.spectralFlatness < shortSpeechFallbackOptions.maximumSpectralFlatness) {
      this.pendingVoiceFrames.push({end: frameEndMs, duration: durationMs * secondary.voicedFraction})
    }
    const secondarySpeech = active && validSecondary && secondary !== undefined
      && secondary.voicedFraction >= shortSpeechFallbackOptions.minimumVoicedFraction
      && secondary.tonalConcentration < shortSpeechFallbackOptions.maximumTonalConcentration
      && secondary.spectralFlatness < shortSpeechFallbackOptions.maximumSpectralFlatness
    if (secondarySpeech && secondary) {
      this.shortSpeechFrames.push({end: frameEndMs, duration: durationMs * secondary.voicedFraction})
    }
    // 背景音がPCM閾値を超え続けても、離れた確率ピークを無期限に合算しない。
    if (!this.confirmed && this.activeFrames.length > 0) {
      this.candidateStart = Math.max(this.activeFrames[0].start, evidenceStart)
    }
    this.strongFrames = this.strongFrames.filter(item => item.end > evidenceStart)
    if (speechProbability >= this.options.strongSpeechProbability) {
      this.strongFrames.push({ end: frameEndMs, duration: durationMs })
      this.neuralSilenceMs = 0
    } else if (this.confirmed && secondarySpeech) {
      // 確定後に主モデルの確率が落ちても、PCM活動と独立した有声・雑音判定が
      // 発話の継続を示すframeを無音へ加算しない。未確定の候補には適用しない。
      this.neuralSilenceMs = 0
    } else if (speechProbability < this.options.negativeSpeechProbability) {
      this.neuralSilenceMs += durationMs
    }
    this.consecutiveHighMs = active && speechProbability >= this.options.highSpeechProbability
      ? Math.min(this.consecutiveHighMs + durationMs, this.options.minimumHighMs) : 0
    this.evidenceFrames = this.evidenceFrames.filter(item => item.end > evidenceStart)
    if (!this.confirmed && active && speechProbability >= this.options.evidenceProbability) {
      this.evidenceFrames.push({end: frameEndMs, duration: durationMs, probability: speechProbability})
    }
    const speechEvidenceMs = this.evidenceFrames.reduce((total, item) =>
      total + item.probability * Math.min(item.duration, item.end - evidenceStart), 0)
    const strongMs = this.strongFrames.reduce((total, item) => (
      total + Math.min(item.duration, item.end - evidenceStart)
    ), 0)
    // 通常はlegacyの4 frame相当を確認する。短い発話はPCM活動を伴う
    // 確率の積分値も根拠にするが、直近2 frameの連続した高確率を必須にする。
    const activeMs = this.activeFrames.reduce((total, item) => (
      total + item.end - Math.max(item.start, evidenceStart)
    ), 0)
    const shortSpeechMs = this.shortSpeechFrames.reduce((total, item) =>
      total + Math.min(item.duration, item.end - evidenceStart), 0)
    // 発話前に誤って止めないよう、未確定の短い候補だけをPCM静音での終了時に補う。
    const shortFallback = frameEndMs - this.lastActiveEnd > this.options.silenceMs
      && this.lastActiveEnd - this.candidateStart <= shortSpeechFallbackOptions.maximumActiveSpanMs
      && shortSpeechMs >= shortSpeechFallbackOptions.minimumVoicedEvidenceMs
    if (!this.confirmed && activeMs >= this.options.minimumActiveMs
      && (shortFallback || strongMs >= this.options.minimumStrongMs
        || ((strongMs >= this.options.shortStrongMs || speechEvidenceMs >= this.options.minimumEvidenceMs) && this.consecutiveHighMs >= this.options.minimumHighMs))) {
      this.confirmed = true
      this.activeFrames = []
      this.emit({ type: 'confirmed', speechStartedAtMs: this.candidateStart, detectedAtMs: frameEndMs })
    }
    // まだ確定できない候補でも、PCM活動と独立した有声根拠が残る間は
    // 続く語の判定を2秒の証拠窓まで待つ。確定条件やpre-rollの上限は変えない。
    // 根拠のない雑音は従来どおり終了し、短い発話の終了時確定も維持する。
    const pendingVoiceMs = this.pendingVoiceFrames.reduce((total, item) =>
      total + Math.min(item.duration, item.end - evidenceStart), 0)
    const pendingSpeech = !this.confirmed && activeMs >= this.options.minimumActiveMs
      && this.lastActiveEnd - this.candidateStart <= shortSpeechFallbackOptions.maximumActiveSpanMs
      && pendingVoiceMs >= shortSpeechFallbackOptions.minimumVoicedEvidenceMs
      && frameEndMs - this.candidateStart < this.options.evidenceWindowMs
    const ended = (frameEndMs - this.lastActiveEnd > this.options.silenceMs && !pendingSpeech)
      || (this.confirmed && this.neuralSilenceMs > this.options.neuralSilenceMs)
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
