import type { VoiceSessionEvent } from '../lib/voice-session/generated'
import { parseVoiceSessionEvent } from '../lib/voice-session/validation'

const sameBytes = (left: Uint8Array, right: Uint8Array): boolean => (
  left.length === right.length && left.every((value, index) => value === right[index])
)

export class CoreEventReceiver {
  private readonly payloads = new Map<string, Uint8Array>()
  private readonly sequences = new Map<string, number>()
  private retainedBytes = 0

  constructor(
    private readonly maxEvents = 256,
    private readonly maxBytes = 1_048_576,
  ) {
    if (maxEvents < 1 || maxBytes < 1) {
      throw new Error('deduplication limits must be positive')
    }
  }

  receive(payload: Uint8Array): { event: VoiceSessionEvent; duplicate: boolean } {
    const event = parseVoiceSessionEvent(
      JSON.parse(new TextDecoder().decode(payload)) as unknown,
    )
    const previous = this.payloads.get(event.event_id)
    if (previous !== undefined) {
      if (!sameBytes(previous, payload)) {
        throw new Error('event_id has a conflicting payload')
      }
      return { event, duplicate: true }
    }
    if (
      this.payloads.size + 1 > this.maxEvents
      || this.retainedBytes + payload.byteLength > this.maxBytes
    ) {
      throw new Error('event deduplication capacity exceeded')
    }
    this.acceptSequence(event)
    const retained = payload.slice()
    this.payloads.set(event.event_id, retained)
    this.retainedBytes += retained.byteLength
    return { event, duplicate: false }
  }

  clear(): void {
    this.payloads.clear()
    this.sequences.clear()
    this.retainedBytes = 0
  }

  private acceptSequence(event: VoiceSessionEvent): void {
    if (event.response_id === undefined) return
    for (const field of ['text_sequence', 'audio_sequence'] as const) {
      const sequence = event[field]
      if (sequence === undefined) continue
      const key = `${event.type}:${event.response_id}:${field}`
      const expected = (this.sequences.get(key) ?? 0) + 1
      if (sequence !== expected) {
        throw new Error(`${field} must be contiguous`)
      }
      this.sequences.set(key, sequence)
    }
  }
}

export type BrowserControlMessage = Readonly<{
  event: VoiceSessionEvent
  responseId?: string
  continuousPrefix?: number
}>

export type PlaybackConfirmation = BrowserControlMessage & Readonly<{
  responseId: string
  continuousPrefix: number
}>

export class PlaybackConfirmationTracker {
  private readonly reportedPrefixes = new Map<string, number>()

  constructor(
    private readonly sessionId: string,
    private readonly monotonicMs: () => number,
    private readonly eventId: () => string,
  ) {}

  create(responseId: string, continuousPrefix: number): PlaybackConfirmation | null {
    if (continuousPrefix < 0) return null
    const previous = this.reportedPrefixes.get(responseId)
    if (previous !== undefined && continuousPrefix <= previous) return null
    const event = parseVoiceSessionEvent({
      protocol_version: '1.0',
      event_id: this.eventId(),
      type: 'playback_completed',
      session_id: this.sessionId,
      response_id: responseId,
      last_played_audio_sequence: continuousPrefix + 1,
      monotonic_timestamp_ms: this.monotonicMs(),
    })
    const confirmation = { event, responseId, continuousPrefix }
    this.reportedPrefixes.set(responseId, continuousPrefix)
    return confirmation
  }
}

type TimerHandle = ReturnType<typeof setTimeout>

export type RetryTimer = Readonly<{
  now(): number
  schedule(callback: () => void, delayMs: number): TimerHandle
  cancel(handle: TimerHandle): void
}>

type BrowserOutboxEntry = {
  message: BrowserControlMessage
  payload: Uint8Array<ArrayBuffer>
  initiallySentAtMs: number
  retryCount: number
  timer: TimerHandle | null
}

const MAX_OUTBOX_EVENTS = 256
const MAX_OUTBOX_BYTES = 1_048_576
const RETRY_DEADLINES_MS = [1_000, 2_000, 4_000] as const

export class BrowserControlOutbox {
  private readonly entries = new Map<string, BrowserOutboxEntry>()
  private bytes = 0

  constructor(
    private readonly publish: (payload: Uint8Array<ArrayBuffer>) => Promise<void>,
    private readonly transportUnavailable: () => void,
    private readonly timer: RetryTimer,
  ) {}

  get eventCount(): number {
    return this.entries.size
  }

  get byteCount(): number {
    return this.bytes
  }

  async enqueue(
    message: BrowserControlMessage,
    encodedPayload: Uint8Array<ArrayBuffer>,
  ): Promise<void> {
    if (
      this.eventCount + 1 > MAX_OUTBOX_EVENTS
      || this.byteCount + encodedPayload.byteLength > MAX_OUTBOX_BYTES
    ) {
      this.transportUnavailable()
      throw new Error('browser control outbox capacity exceeded')
    }
    const eventId = message.event.event_id
    if (this.entries.has(eventId)) {
      throw new Error('browser control outbox event_id already exists')
    }
    const payload = encodedPayload.slice()
    const entry: BrowserOutboxEntry = {
      message,
      payload,
      initiallySentAtMs: this.timer.now(),
      retryCount: 0,
      timer: null,
    }
    this.entries.set(eventId, entry)
    this.bytes += payload.byteLength
    try {
      await this.publish(payload)
    } catch (error) {
      this.remove(eventId)
      throw error
    }
    if (this.entries.get(eventId) === entry) this.scheduleRetry(eventId, entry)
  }

  acknowledge(eventId: string): BrowserControlMessage | null {
    return this.remove(eventId)?.message ?? null
  }

  clear(): void {
    for (const entry of this.entries.values()) {
      if (entry.timer !== null) this.timer.cancel(entry.timer)
    }
    this.entries.clear()
    this.bytes = 0
  }

  private scheduleRetry(eventId: string, entry: BrowserOutboxEntry): void {
    const deadline = entry.initiallySentAtMs + RETRY_DEADLINES_MS[entry.retryCount]
    const delay = Math.max(0, deadline - this.timer.now())
    entry.timer = this.timer.schedule(() => {
      void this.retry(eventId, entry)
    }, delay)
  }

  private async retry(eventId: string, entry: BrowserOutboxEntry): Promise<void> {
    if (this.entries.get(eventId) !== entry) return
    entry.timer = null
    try {
      await this.publish(entry.payload)
    } catch {
      if (this.entries.get(eventId) === entry) this.transportUnavailable()
      return
    }
    if (this.entries.get(eventId) !== entry) return
    entry.retryCount += 1
    if (entry.retryCount === RETRY_DEADLINES_MS.length) {
      this.transportUnavailable()
      return
    }
    this.scheduleRetry(eventId, entry)
  }

  private remove(eventId: string): BrowserOutboxEntry | null {
    const entry = this.entries.get(eventId)
    if (entry === undefined) return null
    if (entry.timer !== null) this.timer.cancel(entry.timer)
    this.entries.delete(eventId)
    this.bytes -= entry.payload.byteLength
    return entry
  }
}
