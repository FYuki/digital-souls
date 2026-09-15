import {afterEach, describe, expect, it, vi} from 'vitest'
import {LiveKitVoiceSessionController, type VoiceSessionRoom} from './livekit/voice-session'
import type {VoiceSessionEvent} from './lib/voice-session/generated'
import {parseVoiceSessionEvent} from './lib/voice-session/validation'

const SESSION = '10000000-0000-4000-8000-000000000001'
const USER = '20000000-0000-4000-8000-000000000001'
const CONTEXT = {characterId: 'miori', conversationId: '30000000-0000-4000-8000-000000000001'}

function harness(autoAck = true) {
  let receive = (_event: VoiceSessionEvent) => {}
  let publication = 0, generation = 0
  const events: VoiceSessionEvent[] = []
  const track = {enabled: true}
  const stream = {getAudioTracks: () => [track]} as unknown as MediaStream
  const reply = (request: VoiceSessionEvent, changed: Partial<VoiceSessionEvent> = {}) => {
    receive(parseVoiceSessionEvent({
      type: 'audio_input_opened', protocol_version: '2.0', event_id: crypto.randomUUID(),
      session_id: SESSION, monotonic_timestamp_ms: 1, request_event_id: request.event_id,
      track_sid: request.track_sid, input_revision: request.input_revision,
      input_generation: ++generation, ...changed,
    }))
  }
  const room: VoiceSessionRoom = {
    connect: vi.fn(async () => {}),
    publishMicrophone: vi.fn(async () => 'TR_' + (++publication)),
    muteMicrophone: vi.fn(async () => {}),
    publishControlEvent: vi.fn(async event => {
      parseVoiceSessionEvent(event)
      events.push(event)
      if (autoAck && event.type === 'audio_input_open_requested') reply(event)
    }),
    stopPlayback: vi.fn(() => 0), disconnect: vi.fn(),
  }
  const controller = new LiveKitVoiceSessionController(() => {}, () => {}, {
    requestToken: vi.fn(async () => ({
      session_id: SESSION, participant_id: USER, room: 'voice', token: 'test-token',
      livekit_url: 'ws://localhost:7880', expires_at: '2026-09-15T00:00:00Z', reconnect_grace_ms: 60000,
    })),
    endSession: vi.fn(async () => {}),
    roomFactory: (_observe, onEvent) => {receive = onEvent; return room},
    eventId: () => crypto.randomUUID(), monotonicMs: () => 1,
  })
  return {controller, room, stream, track, events, reply,
    receive: (event: VoiceSessionEvent) => receive(event),
    opens: () => events.filter(event => event.type === 'audio_input_open_requested'),
  }
}

afterEach(() => vi.useRealTimers())

describe('Backend microphone authority', () => {
  it('keeps the device suppressed until matching input authorization arrives', async () => {
    const h = harness(false)
    await h.controller.ensureSession(CONTEXT)
    const opening = h.controller.resumeMicrophone(h.stream)
    await vi.waitFor(() => expect(h.opens()).toHaveLength(1))
    expect(h.track.enabled).toBe(false)
    h.reply(h.opens()[0], {request_event_id: crypto.randomUUID()})
    expect(h.track.enabled).toBe(false)
    h.reply(h.opens()[0])
    await opening
    expect(h.track.enabled).toBe(true)
    expect(h.controller.snapshot().input).toBe('listening')
    expect(h.events.some(event => event.type === 'speech_started' || event.type === 'speech_stopped')).toBe(false)
    await h.controller.end()
  })

  it('republishes a different SID after focus suppression and rejects late authorization', async () => {
    const h = harness()
    await h.controller.ensureSession(CONTEXT)
    await h.controller.resumeMicrophone(h.stream)
    const old = h.opens()[0]
    await h.controller.setTextInputFocused(CONTEXT, true)
    expect(h.track.enabled).toBe(false)
    h.reply(old)
    expect(h.track.enabled).toBe(false)
    await h.controller.setTextInputFocused(CONTEXT, false)
    expect(h.opens()).toHaveLength(2)
    expect(h.opens()[1].track_sid).not.toBe(old.track_sid)
    expect(h.track.enabled).toBe(true)
    await h.controller.end()
  })

  it('does not unmute a manual mute on blur', async () => {
    const h = harness()
    await h.controller.ensureSession(CONTEXT)
    await h.controller.resumeMicrophone(h.stream)
    await h.controller.setTextInputFocused(CONTEXT, true)
    await h.controller.muteMicrophone()
    await h.controller.setTextInputFocused(CONTEXT, false)
    expect(h.track.enabled).toBe(false)
    expect(h.opens()).toHaveLength(1)
    expect(h.controller.snapshot().input).toBe('muted')
    await h.controller.end()
  })

  it('times out without enabling the microphone and ignores the late ACK', async () => {
    vi.useFakeTimers()
    const h = harness(false)
    await h.controller.ensureSession(CONTEXT)
    const opening = h.controller.resumeMicrophone(h.stream)
    const rejected = expect(opening).rejects.toThrow('開始を確認できません')
    await vi.advanceTimersByTimeAsync(1)
    expect(h.opens()).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(5000)
    await rejected
    expect(h.track.enabled).toBe(false)
    h.reply(h.opens()[0])
    expect(h.track.enabled).toBe(false)
    expect(h.controller.snapshot().input).toBe('muted')
    await h.controller.end()
  })

  it('uses Backend speech state and has no client speech injection methods', async () => {
    const h = harness()
    await h.controller.ensureSession(CONTEXT)
    await h.controller.resumeMicrophone(h.stream)
    const request = h.opens()[0]
    h.receive(parseVoiceSessionEvent({
      type: 'speech_stopped', protocol_version: '2.0', session_id: SESSION, event_id: crypto.randomUUID(),
      utterance_id: crypto.randomUUID(), speaker: {participant_id: USER, role: 'user'},
      monotonic_timestamp_ms: 400, input_generation: 1, track_sid: request.track_sid,
      start_sample: 1600, active_end_sample: 3200, detected_sample: 16000, sample_rate: 16000,
      clock_domain: 'server_monotonic',
    }))
    expect(h.controller.snapshot().input).toBe('transcribing')
    expect('speechStarted' in h.controller).toBe(false)
    expect('speechStopped' in h.controller).toBe(false)
    await h.controller.end()
  })
})
