import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { parseVoiceSessionEvent } from './validation'

const fixture = JSON.parse(readFileSync(
  resolve(process.cwd(), '../contracts/voice-session/fixtures/text-input.json'), 'utf8',
)) as { valid: unknown[]; invalid: unknown[] }

describe('テキスト入力のFE/BE共有契約', () => {
  it.each(fixture.valid)('新しい入力・結果・抑止eventを受理する: %j', (event) => {
    expect(parseVoiceSessionEvent(event)).toEqual(event)
  })

  it.each(fixture.invalid)('旧version、不正入力、不正sourceを拒否する: %j', (event) => {
    expect(() => parseVoiceSessionEvent(event)).toThrow()
  })
})
