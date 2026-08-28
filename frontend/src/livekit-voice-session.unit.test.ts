import { describe, expect, test, vi } from 'vitest'

import type { VoiceSessionEvent } from './lib/voice-session/generated'
import {
  LiveKitVoiceSessionController,
  type VoiceSessionDependencies,
  type VoiceSessionRoom,
  type VoiceSessionSnapshot,
} from './livekit/voice-session'

const SESSION_ID = '20000000-0000-4000-8000-000000000001'
const PARTICIPANT_ID = '40000000-0000-4000-8000-000000000001'
const UTTERANCE_ID = '30000000-0000-4000-8000-000000000001'

const setup = () => {
  const events: VoiceSessionEvent[] = []
  const snapshots: VoiceSessionSnapshot[] = []
  const observations: Array<(value: {
    transport: 'available' | 'unavailable' | 'idle'
    control: 'available' | 'unavailable'
    audio: 'available' | 'unavailable'
  }) => void> = []
  const room: VoiceSessionRoom = {
    connect: vi.fn(async () => undefined),
    publishMicrophone: vi.fn(async () => undefined),
    muteMicrophone: vi.fn(async () => undefined),
    publishControlEvent: vi.fn(async (event) => { events.push(event) }),
    disconnect: vi.fn(),
  }
  let eventIndex = 1
  const dependencies: VoiceSessionDependencies = {
    requestToken: vi.fn(async () => ({
      session_id: SESSION_ID,
      participant_id: PARTICIPANT_ID,
      room: 'voice-room',
      token: 'token',
      livekit_url: 'ws://127.0.0.1:7880',
      expires_at: '2026-08-28T00:00:00Z',
      reconnect_grace_ms: 60_000,
    })),
    endSession: vi.fn(async () => undefined),
    roomFactory: (observe) => {
      observations.push(observe)
      return room
    },
    eventId: () => `10000000-0000-4000-8000-${String(eventIndex++).padStart(12, '0')}`,
    monotonicMs: () => 1_000,
  }
  const controller = new LiveKitVoiceSessionController(
    (snapshot) => snapshots.push(snapshot),
    () => undefined,
    dependencies,
  )
  return { controller, dependencies, events, observations, room, snapshots }
}

describe('通常会話UI向けLiveKit音声session', () => {
  test('sessionとutteranceを分離し、mute後も同じsessionを再開する', async () => {
    const { controller, dependencies, events, room } = setup()
    const context = { characterId: 'miori', conversationId: 'conversation-id' }

    await controller.ensureSession(context)
    await controller.resumeMicrophone()
    await controller.speechStarted(UTTERANCE_ID, 1_010)
    await controller.speechStopped(UTTERANCE_ID, 1_020)
    await controller.muteMicrophone()
    await controller.resumeMicrophone()

    expect(dependencies.requestToken).toHaveBeenCalledTimes(1)
    expect(room.publishMicrophone).toHaveBeenCalledTimes(2)
    expect(room.muteMicrophone).toHaveBeenCalledTimes(1)
    expect(events.map((event) => event.type)).toEqual([
      'session_start_requested',
      'session_resumed',
      'speech_started',
      'speech_stopped',
      'observation',
      'session_muted',
      'session_resumed',
    ])
    expect(events[2]).toMatchObject({
      session_id: SESSION_ID,
      utterance_id: UTTERANCE_ID,
      speaker: { participant_id: PARTICIPANT_ID, role: 'user' },
    })
    expect(controller.snapshot().phase).toBe('listening')
  })

  test('reconnect中の表示状態を経て直前のmute状態へ戻る', async () => {
    const { controller, observations } = setup()
    await controller.ensureSession({
      characterId: 'miori',
      conversationId: 'conversation-id',
    })

    observations[0]({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
    expect(controller.snapshot().phase).toBe('reconnecting')
    observations[0]({ transport: 'available', control: 'available', audio: 'available' })

    expect(controller.snapshot().phase).toBe('muted')
  })

  test('conversation切替では旧Roomを終了して新sessionを作る', async () => {
    const { controller, dependencies, room } = setup()
    await controller.ensureSession({ characterId: 'miori', conversationId: 'one' })
    await controller.ensureSession({ characterId: 'miori', conversationId: 'two' })

    expect(room.disconnect).toHaveBeenCalledTimes(1)
    expect(dependencies.endSession).toHaveBeenCalledWith(SESSION_ID)
    expect(dependencies.requestToken).toHaveBeenCalledTimes(2)
  })

  test('microphone track publish失敗後も同じsessionで再試行できる', async () => {
    const { controller, room } = setup()
    vi.mocked(room.publishMicrophone)
      .mockRejectedValueOnce(new Error('permission or publish failure'))
      .mockResolvedValueOnce(undefined)
    await controller.ensureSession({
      characterId: 'miori', conversationId: 'conversation-id',
    })

    await expect(controller.resumeMicrophone()).rejects.toThrow(
      'permission or publish failure',
    )
    expect(controller.snapshot().phase).toBe('muted')

    await controller.resumeMicrophone()
    expect(controller.snapshot().phase).toBe('listening')
    expect(room.publishMicrophone).toHaveBeenCalledTimes(2)
  })

  test('明示終了ではRoomを切断しBackend sessionを一度だけ終了する', async () => {
    const { controller, dependencies, room } = setup()
    await controller.ensureSession({
      characterId: 'miori', conversationId: 'conversation-id',
    })

    await controller.end()
    await controller.end()

    expect(room.disconnect).toHaveBeenCalledTimes(1)
    expect(dependencies.endSession).toHaveBeenCalledTimes(1)
    expect(dependencies.endSession).toHaveBeenCalledWith(SESSION_ID)
    expect(controller.snapshot()).toEqual({
      phase: 'idle', context: null, sessionId: null,
    })
  })
})
