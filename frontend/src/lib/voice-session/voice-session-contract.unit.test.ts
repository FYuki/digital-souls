import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const contractRoot = resolve(process.cwd(), '..', 'contracts', 'voice-session')

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(
    readFileSync(resolve(contractRoot, 'fixtures', name), 'utf-8'),
  ) as Record<string, unknown>
}

function normalResponseEvent(eventType: string): Record<string, unknown> {
  const events = fixture('normal.json').events as Array<Record<string, unknown>>
  const event = events.find((candidate) => candidate.type === eventType)
  if (event === undefined) {
    throw new Error(`normal fixtureに${eventType}がありません`)
  }
  return event
}

async function loadValidationModule(): Promise<typeof import('./validation')> {
  const modulePath = './validation'
  return import(/* @vite-ignore */ modulePath)
}

describe('voice session shared contract', () => {
  it.each(['normal.json', 'cancel-race.json'])(
    '共有fixture %s のeventを境界validation後だけ受理する',
    async (fixtureName) => {
      const { parseVoiceSessionEvent } = await loadValidationModule()
      const events = fixture(fixtureName).events
      expect(Array.isArray(events)).toBe(true)
      expect((events as unknown[]).map(parseVoiceSessionEvent)).toHaveLength(
        (events as unknown[]).length,
      )
    },
  )

  it.each(['response_delta', 'response_audio_chunk'])(
    '%sの順序どおりのtext rangeと空区間を受理する',
    async (eventType) => {
      const { parseVoiceSessionEvent } = await loadValidationModule()
      const event = normalResponseEvent(eventType)

      const parsed = parseVoiceSessionEvent(event)
      const emptyRangeParsed = parseVoiceSessionEvent({
        ...event,
        text_range: { start: 4, end: 4 },
      })

      expect(parsed.text_range).toEqual({ start: 0, end: 4 })
      expect(emptyRangeParsed.text_range).toEqual({ start: 4, end: 4 })
      expect(event.text_range).toEqual({ start: 0, end: 4 })
    },
  )

  it.each(['response_delta', 'response_audio_chunk'])(
    '%sの逆順text rangeを拒否する',
    async (eventType) => {
      const { parseVoiceSessionEvent } = await loadValidationModule()
      const event = normalResponseEvent(eventType)

      expect(() =>
        parseVoiceSessionEvent({
          ...event,
          text_range: { start: 4, end: 3 },
        }),
      ).toThrow()
      expect(event.text_range).toEqual({ start: 0, end: 4 })
    },
  )

  it('speech停止と応答開始を分離しpending発話を次の応答へ結合する', () => {
    const loaded = fixture('normal.json')
    const events = loaded.events as Array<Record<string, unknown>>
    const expected = loaded.expected as Record<string, unknown>
    const pendingUtteranceId = expected.pending_utterance_id
    const expectedSources = expected.response_source_utterance_ids
    const stoppedIndex = events.findIndex(
      (event) =>
        event.type === 'speech_stopped' &&
        event.utterance_id === pendingUtteranceId,
    )
    const pendingIndex = events.findIndex(
      (event) =>
        event.type === 'utterance_pending' &&
        event.utterance_id === pendingUtteranceId,
    )
    const responseIndex = events.findIndex(
      (event) => event.type === 'response_started',
    )
    const responseStarted = events[responseIndex]

    expect(stoppedIndex).toBeGreaterThanOrEqual(0)
    expect(pendingIndex).toBeGreaterThan(stoppedIndex)
    expect(responseIndex).toBeGreaterThan(pendingIndex)
    expect(
      events
        .slice(stoppedIndex + 1, pendingIndex + 1)
        .some((event) => event.type === 'response_started'),
    ).toBe(false)
    expect(responseStarted.source_utterance_ids).toEqual(expectedSources)
    expect(responseStarted.source_utterance_ids).toContain(pendingUtteranceId)
  })

  it('cancel確定後の遅延deltaを破棄して会話状態を変えない', () => {
    const loaded = fixture('cancel-race.json')
    const events = loaded.events as Array<Record<string, unknown>>
    const expected = loaded.expected as Record<string, unknown>
    let state = { terminal: null as string | null, text: '' }
    let stateBeforeLateEvent: typeof state | null = null

    for (const event of events) {
      if (state.terminal !== null) {
        stateBeforeLateEvent = { ...state }
        continue
      }
      if (event.type === 'response_delta') {
        state = { ...state, text: state.text + (event.text as string) }
      } else if (
        ['response_cancelled', 'response_completed', 'response_failed'].includes(
          event.type as string,
        )
      ) {
        state = { ...state, terminal: event.type as string }
      }
    }

    expect(stateBeforeLateEvent).not.toBeNull()
    expect(state).toEqual(stateBeforeLateEvent)
    expect(state.terminal).toBe(expected.terminal_event)
    expect(expected.late_event_discarded).toBe(true)
  })

  it('非互換protocolをtyped eventへ変換しない', async () => {
    const { parseVoiceSessionEvent } = await loadValidationModule()
    const loaded = fixture('protocol-version-mismatch.json')

    expect(() => parseVoiceSessionEvent(loaded.event)).toThrow()
  })

  it('JavaScript安全整数上限を超えるeventをtyped eventへ変換しない', async () => {
    const { parseVoiceSessionEvent } = await loadValidationModule()
    const loaded = fixture('unsafe-integer.json')

    expect(() => parseVoiceSessionEvent(loaded.event)).toThrow()
  })
})
