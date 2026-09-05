import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { parseScreenPerceptionEvent } from './validation'

const contractRoot = resolve(process.cwd(), '..', 'contracts', 'perception', 'screen')

function loadJson(path: string): unknown {
  return JSON.parse(readFileSync(path, 'utf-8')) as unknown
}

describe('screen perception shared contract', () => {
  it('正常fixtureをFrontend境界でも受理する', () => {
    const fixtureRoot = resolve(contractRoot, 'fixtures', 'valid')
    for (const name of readdirSync(fixtureRoot).sort()) {
      const fixture = loadJson(resolve(fixtureRoot, name)) as { events: unknown[] }
      expect(fixture.events.map(parseScreenPerceptionEvent)).toHaveLength(fixture.events.length)
    }
  })

  it('異常fixtureをすべて拒否する', () => {
    const fixtureRoot = resolve(contractRoot, 'fixtures', 'invalid')
    for (const name of readdirSync(fixtureRoot).sort()) {
      const event = loadJson(resolve(fixtureRoot, name))
      expect(() => parseScreenPerceptionEvent(event), name).toThrow()
    }
  })

  it('各辺の上限内でも総pixel上限を超える画像を拒否する', () => {
    const fixture = loadJson(
      resolve(contractRoot, 'fixtures', 'valid', 'normal-session.json'),
    ) as { events: Array<Record<string, unknown>> }
    const metadata = fixture.events.find(
      (event) => event.type === 'screen_snapshot_upload_metadata',
    )
    expect(metadata).toBeDefined()
    expect(() => parseScreenPerceptionEvent({
      ...metadata,
      width: 2560,
      height: 2560,
    })).toThrow(/pixel limit/)
  })

  it('暦上存在しないUTC日時を拒否する', () => {
    const fixture = loadJson(
      resolve(contractRoot, 'fixtures', 'valid', 'normal-session.json'),
    ) as { events: Array<Record<string, unknown>> }
    const request = fixture.events.find(
      (event) => event.type === 'screen_snapshot_requested',
    )
    expect(request).toBeDefined()
    expect(() => parseScreenPerceptionEvent({
      ...request,
      requested_at: '2026-02-30T03:00:06Z',
    })).toThrow(/protocol 1.0/)
  })
})
