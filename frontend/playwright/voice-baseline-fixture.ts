import { createHash } from 'node:crypto'

export const BASELINE_FIXTURE_VERSION = 'speech-v2'

export type BaselineFixture = {
  fixture_version: typeof BASELINE_FIXTURE_VERSION
  audio_sha256: string
  sample_rate_hz: number
  speech_start_sample: number
  speech_end_sample: number
  expected_transcript: string
}

const requiredField = (value: Record<string, unknown>, key: string): unknown => {
  if (!(key in value)) throw new Error(`baseline fixture ${key} is required`)
  return value[key]
}

const requiredString = (value: Record<string, unknown>, key: string): string => {
  const candidate = requiredField(value, key)
  if (typeof candidate !== 'string' || candidate.length === 0) {
    throw new Error(`baseline fixture ${key} must be a non-empty string`)
  }
  return candidate
}

const requiredInteger = (value: Record<string, unknown>, key: string): number => {
  const candidate = requiredField(value, key)
  if (!Number.isInteger(candidate) || Number(candidate) < 0) {
    throw new Error(`baseline fixture ${key} must be a non-negative integer`)
  }
  return Number(candidate)
}

const wavProperties = (audio: Uint8Array) => {
  if (audio.byteLength < 44) throw new Error('baseline fixture WAV is truncated')
  const header = new TextDecoder('ascii').decode(audio.subarray(0, 12))
  if (!header.startsWith('RIFF') || header.slice(8) !== 'WAVE') {
    throw new Error('baseline fixture must be a RIFF WAVE file')
  }
  const view = new DataView(audio.buffer, audio.byteOffset, audio.byteLength)
  let offset = 12
  let sampleRate: number | null = null
  let blockAlign: number | null = null
  let dataBytes: number | null = null
  while (offset + 8 <= audio.byteLength) {
    const chunkName = new TextDecoder('ascii').decode(audio.subarray(offset, offset + 4))
    const chunkSize = view.getUint32(offset + 4, true)
    const bodyOffset = offset + 8
    if (bodyOffset + chunkSize > audio.byteLength) {
      throw new Error('baseline fixture WAV chunk is truncated')
    }
    if (chunkName === 'fmt ') {
      if (chunkSize < 16 || view.getUint16(bodyOffset, true) !== 1) {
        throw new Error('baseline fixture must use PCM WAV encoding')
      }
      sampleRate = view.getUint32(bodyOffset + 4, true)
      blockAlign = view.getUint16(bodyOffset + 12, true)
    }
    if (chunkName === 'data') dataBytes = chunkSize
    offset = bodyOffset + chunkSize + (chunkSize % 2)
  }
  if (sampleRate === null || blockAlign === null || blockAlign === 0 || dataBytes === null) {
    throw new Error('baseline fixture WAV is missing format or audio data')
  }
  return { sampleRate, sampleCount: dataBytes / blockAlign }
}

export const validateBaselineFixture = (
  rawMetadata: unknown,
  audio: Uint8Array,
): BaselineFixture => {
  if (typeof rawMetadata !== 'object' || rawMetadata === null || Array.isArray(rawMetadata)) {
    throw new Error('baseline fixture metadata must be an object')
  }
  const metadata = rawMetadata as Record<string, unknown>
  const fixtureVersion = requiredString(metadata, 'fixture_version')
  if (fixtureVersion !== BASELINE_FIXTURE_VERSION) {
    throw new Error(`baseline fixture version must be ${BASELINE_FIXTURE_VERSION}`)
  }
  const audioSha256 = requiredString(metadata, 'audio_sha256')
  if (!/^[0-9a-f]{64}$/.test(audioSha256)) {
    throw new Error('baseline fixture audio_sha256 must be a lowercase SHA-256 digest')
  }
  const actualSha256 = createHash('sha256').update(audio).digest('hex')
  if (actualSha256 !== audioSha256) throw new Error('baseline fixture audio_sha256 does not match WAV')

  const sampleRateHz = requiredInteger(metadata, 'sample_rate_hz')
  const speechStartSample = requiredInteger(metadata, 'speech_start_sample')
  const speechEndSample = requiredInteger(metadata, 'speech_end_sample')
  const expectedTranscript = requiredString(metadata, 'expected_transcript')
  const wav = wavProperties(audio)
  if (sampleRateHz !== wav.sampleRate) {
    throw new Error('baseline fixture sample_rate_hz does not match WAV')
  }
  if (!(speechStartSample < speechEndSample && speechEndSample <= wav.sampleCount)) {
    throw new Error('baseline fixture speech boundaries are invalid')
  }
  return {
    fixture_version: BASELINE_FIXTURE_VERSION,
    audio_sha256: audioSha256,
    sample_rate_hz: sampleRateHz,
    speech_start_sample: speechStartSample,
    speech_end_sample: speechEndSample,
    expected_transcript: expectedTranscript,
  }
}

export const normalizeBaselineTranscript = (value: string): string => (
  value.normalize('NFKC').trim().replace(/\s+/g, ' ')
)
