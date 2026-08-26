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
  it.each([
    'normal.json',
    'cancel-race.json',
    'duplicate.json',
    'out-of-order.json',
    'reconnect.json',
  ])(
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

  it.each(['response_delta', 'response_audio_segment'])(
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

  it.each(['response_delta', 'response_audio_segment'])(
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

  it('重複event_idの副作用を一度だけ適用する', () => {
    const loaded = fixture('duplicate.json')
    const events = loaded.events as Array<Record<string, unknown>>
    const expected = loaded.expected as Record<string, unknown>
    const appliedEventIds = new Set<string>()
    let assistantText = ''

    for (const event of events) {
      const eventId = event.event_id as string
      if (appliedEventIds.has(eventId)) continue
      appliedEventIds.add(eventId)
      if (event.type === 'response_delta') assistantText += event.text as string
    }

    expect([...appliedEventIds].sort()).toEqual(expected.applied_event_ids)
    expect(assistantText).toBe(expected.assistant_text)
  })

  it('同じevent_idでpayloadが異なる重複をprotocol errorとする', () => {
    const loaded = fixture('duplicate.json')
    const events = loaded.events as Array<Record<string, unknown>>
    const conflicting = loaded.conflicting_event as Record<string, unknown>
    const expected = loaded.expected as Record<string, unknown>
    const receivedPayloadByEventId = new Map<string, Record<string, unknown>>()

    for (const event of events) {
      const eventId = event.event_id as string
      const previous = receivedPayloadByEventId.get(eventId)
      if (previous === undefined) {
        receivedPayloadByEventId.set(eventId, event)
        continue
      }
      expect(event).toEqual(previous)
    }

    const classification = JSON.stringify(
      receivedPayloadByEventId.get(conflicting.event_id as string),
    ) === JSON.stringify(conflicting)
      ? 'duplicate'
      : 'terminal_protocol_error'
    expect(classification).toBe(
      expected.conflicting_duplicate,
    )
  })

  it('text sequenceの欠番を越えてstreamを進めない', () => {
    const loaded = fixture('out-of-order.json')
    const events = loaded.events as Array<Record<string, unknown>>
    const expected = loaded.expected as Record<string, unknown>
    let nextSequence = 1
    let lastContiguousBeforeRecovery = 0

    for (const event of events.slice(0, 2)) {
      const sequence = event.text_sequence as number
      if (sequence !== nextSequence) break
      lastContiguousBeforeRecovery = sequence
      nextSequence += 1
    }

    expect(lastContiguousBeforeRecovery).toBe(
      expected.last_contiguous_sequence_before_recovery,
    )
    expect(expected.sequence_3_not_applied_before_sequence_2).toBe(true)
  })

  it('再接続でsessionを維持し旧responseの遅延eventを破棄する', () => {
    const loaded = fixture('reconnect.json')
    const events = loaded.events as Array<Record<string, unknown>>
    const expected = loaded.expected as Record<string, unknown>
    const terminalResponseIds = new Set<string>()
    const lateEvents: Array<Record<string, unknown>> = []

    for (const event of events) {
      const responseId = event.response_id
      if (
        typeof responseId === 'string'
        && terminalResponseIds.has(responseId)
      ) {
        lateEvents.push(event)
        continue
      }
      if (
        ['response_cancelled', 'response_completed', 'response_failed'].includes(
          event.type as string,
        )
        && typeof responseId === 'string'
      ) terminalResponseIds.add(responseId)
    }

    expect(new Set(events.map((event) => event.session_id))).toEqual(
      new Set([expected.session_id_preserved]),
    )
    expect(lateEvents.map((event) => event.type)).toEqual(['response_delta'])
    expect(expected.late_old_response_event_discarded).toBe(true)
  })

  it('TTFAをclient monotonic内だけで算出する', () => {
    const loaded = fixture('normal.json')
    const events = loaded.events as Array<Record<string, unknown>>
    const expected = loaded.expected as Record<string, unknown>
    const observations = new Map(
      events
        .filter((event) => event.type === 'observation')
        .map((event) => [event.measurement, event]),
    )
    const speechStopped = observations.get('speech_stopped')
    const playbackStarted = observations.get('playback_started')
    const firstAudioOut = observations.get('first_audio_out')

    expect(speechStopped?.clock_domain).toBe('client_monotonic')
    expect(playbackStarted?.clock_domain).toBe('client_monotonic')
    expect(
      (playbackStarted?.timestamp as number)
      - (speechStopped?.timestamp as number),
    ).toBe(expected.ttfa_ms)
    expect(firstAudioOut?.clock_domain).toBe('server_monotonic')
    expect(firstAudioOut?.unit).toBe('nanosecond')
  })

  it('計測点と異なるclock domainを拒否する', async () => {
    const { parseVoiceSessionEvent } = await loadValidationModule()
    const events = fixture('normal.json').events as Array<Record<string, unknown>>
    const playbackObservation = events.find(
      (event) =>
        event.type === 'observation'
        && event.measurement === 'playback_started',
    )

    expect(() => parseVoiceSessionEvent({
      ...playbackObservation,
      clock_domain: 'server_monotonic',
      unit: 'nanosecond',
    })).toThrow()
  })

  it('characterだけにcharacter_idを必須とする', async () => {
    const { parseVoiceSessionEvent } = await loadValidationModule()
    const responseStarted = normalResponseEvent('response_started')
    const speechStarted = (fixture('normal.json').events as Array<Record<string, unknown>>)
      .find((event) => event.type === 'speech_started') as Record<string, unknown>
    const characterWithoutId = Object.fromEntries(
      Object.entries(responseStarted.speaker as Record<string, unknown>)
        .filter(([key]) => key !== 'character_id'),
    )

    expect(() => parseVoiceSessionEvent({
      ...responseStarted,
      speaker: characterWithoutId,
    })).toThrow()
    expect(() => parseVoiceSessionEvent({
      ...speechStarted,
      speaker: {
        ...(speechStarted.speaker as Record<string, unknown>),
        character_id: 'miori',
      },
    })).toThrow()
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
