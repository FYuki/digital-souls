import { createHash } from 'node:crypto'

import { describe, expect, test } from 'vitest'

import {
  normalizeBaselineTranscript,
  validateBaselineFixture,
} from './voice-baseline-fixture'

const pcmWav = (): Uint8Array => {
  const samples = 100
  const bytes = new Uint8Array(44 + samples * 2)
  const view = new DataView(bytes.buffer)
  const writeText = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index += 1) {
      bytes[offset + index] = value.charCodeAt(index)
    }
  }
  writeText(0, 'RIFF')
  view.setUint32(4, bytes.byteLength - 8, true)
  writeText(8, 'WAVE')
  writeText(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, 48_000, true)
  view.setUint32(28, 96_000, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeText(36, 'data')
  view.setUint32(40, samples * 2, true)
  return bytes
}

const metadataFor = (audio: Uint8Array) => ({
  fixture_version: 'speech-v2',
  audio_sha256: createHash('sha256').update(audio).digest('hex'),
  sample_rate_hz: 48_000,
  speech_start_sample: 10,
  speech_end_sample: 90,
  expected_transcript: 'こんにちは',
})

describe('controlled baseline fixture validation', () => {
  test('accepts the fixed version, WAV identity, boundaries, and transcript label', () => {
    const audio = pcmWav()

    const fixture = validateBaselineFixture(metadataFor(audio), audio)

    expect(fixture).toEqual(metadataFor(audio))
  })

  test.each([
    ['hash', (metadata: ReturnType<typeof metadataFor>) => ({ ...metadata, audio_sha256: '0'.repeat(64) })],
    ['version', (metadata: ReturnType<typeof metadataFor>) => ({ ...metadata, fixture_version: 'speech-v1' })],
    ['transcript', (metadata: ReturnType<typeof metadataFor>) => {
      const { expected_transcript: _removed, ...withoutTranscript } = metadata
      return withoutTranscript
    }],
    ['sample rate', (metadata: ReturnType<typeof metadataFor>) => ({ ...metadata, sample_rate_hz: 16_000 })],
    ['speech boundary', (metadata: ReturnType<typeof metadataFor>) => ({ ...metadata, speech_end_sample: 101 })],
  ])('rejects an invalid %s before trials start', (_name, change) => {
    const audio = pcmWav()

    expect(() => validateBaselineFixture(change(metadataFor(audio)), audio)).toThrow()
  })

  test('normalizes Unicode, surrounding whitespace, and repeated whitespace', () => {
    expect(normalizeBaselineTranscript('  Ｔｅｓｔ\n  transcript  ')).toBe('Test transcript')
  })
})
