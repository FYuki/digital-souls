import { describe, expect, test, vi } from 'vitest'

import type { VoiceSessionEvent } from './lib/voice-session/generated'
import type { RoomObservation } from './livekit/room'
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
  const observations: Array<(value: RoomObservation) => void> = []
  const coreEventReceivers: Array<(event: VoiceSessionEvent) => void> = []
  const room: VoiceSessionRoom = {
    connect: vi.fn(async () => undefined),
    publishMicrophone: vi.fn(async () => undefined),
    muteMicrophone: vi.fn(async () => undefined),
    stopPlayback: vi.fn(() => 0),
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
    roomFactory: (observe, receiveCoreEvent) => {
      observations.push(observe)
      coreEventReceivers.push(receiveCoreEvent)
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
  return {
    controller,
    coreEventReceivers,
    dependencies,
    events,
    observations,
    room,
    snapshots,
  }
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

  test('reconnect後は独立したmicrophone状態からlisteningへ戻る', async () => {
    const { controller, observations } = setup()
    await controller.ensureSession({
      characterId: 'miori', conversationId: 'conversation-id',
    })
    await controller.resumeMicrophone()

    observations[0]({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
    observations[0]({ transport: 'available', control: 'available', audio: 'available' })

    expect(controller.snapshot().phase).toBe('listening')
  })

  test('前sessionのmicrophone状態を次sessionへ持ち越さない', async () => {
    const { controller, observations } = setup()
    await controller.ensureSession({ characterId: 'miori', conversationId: 'one' })
    await controller.resumeMicrophone()
    await controller.end()
    await controller.ensureSession({ characterId: 'miori', conversationId: 'two' })

    observations[1]({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
    observations[1]({ transport: 'available', control: 'available', audio: 'available' })

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

  test('生成・再生中のspeech startでlocal停止後にcancelを一度だけ送る', async () => {
    const { controller, coreEventReceivers, events, observations, room } = setup()
    await controller.ensureSession({
      characterId: 'miori', conversationId: 'conversation-id',
    })
    await controller.resumeMicrophone()
    coreEventReceivers[0]({
      type: 'response_started',
      response_id: '50000000-0000-4000-8000-000000000001',
      source_utterance_ids: [UTTERANCE_ID],
    } as VoiceSessionEvent)
    observations[0]({
      transport: 'available', control: 'available', audio: 'available',
      activeResponseId: '50000000-0000-4000-8000-000000000001',
      renderedEnergy: 1,
    })

    await controller.speechStarted(UTTERANCE_ID, 1_010)
    await controller.speechStarted(
      '30000000-0000-4000-8000-000000000002',
      1_011,
    )

    expect(room.stopPlayback).toHaveBeenCalledTimes(1)
    expect(events.slice(2).map((event) => event.type)).toEqual([
      'playback_stopped',
      'response_cancel_requested',
      'speech_started',
      'speech_started',
    ])
    expect(controller.snapshot()).toMatchObject({
      response: 'interrupting', playback: 'stopped',
    })
  })

  test('生成完了後のspeech startは再生だけを止めて履歴完成状態を維持する', async () => {
    const { controller, coreEventReceivers, events, observations, room } = setup()
    await controller.ensureSession({
      characterId: 'miori', conversationId: 'conversation-id',
    })
    coreEventReceivers[0]({
      type: 'response_started',
      response_id: '50000000-0000-4000-8000-000000000001',
      source_utterance_ids: [UTTERANCE_ID],
    } as VoiceSessionEvent)
    observations[0]({
      transport: 'available', control: 'available', audio: 'available',
      activeResponseId: '50000000-0000-4000-8000-000000000001',
      renderedEnergy: 1,
    })
    coreEventReceivers[0]({
      type: 'response_completed',
      response_id: '50000000-0000-4000-8000-000000000001',
    } as VoiceSessionEvent)

    await controller.speechStarted(UTTERANCE_ID, 1_010)

    expect(room.stopPlayback).toHaveBeenCalledTimes(1)
    expect(events.map((event) => event.type)).toEqual([
      'session_start_requested', 'playback_stopped', 'speech_started',
    ])
    expect(controller.snapshot().response).toBe('idle')
  })

  test('生成完了が最初のaudio renderより先でも再生完了を追跡する', async () => {
    const { controller, coreEventReceivers, observations } = setup()
    const responseId = '50000000-0000-4000-8000-000000000001'
    await controller.ensureSession({
      characterId: 'miori', conversationId: 'conversation-id',
    })
    coreEventReceivers[0]({
      type: 'response_started',
      response_id: responseId,
      source_utterance_ids: [UTTERANCE_ID],
    } as VoiceSessionEvent)
    coreEventReceivers[0]({
      type: 'response_completed',
      response_id: responseId,
      last_audio_sequence: 2,
    } as VoiceSessionEvent)

    observations[0]({
      transport: 'available', control: 'available', audio: 'available',
      activeResponseId: responseId, renderedEnergy: 1, playedPrefix: 0,
    })
    expect(controller.snapshot().playback).toBe('playing')

    observations[0]({
      transport: 'available', control: 'available', audio: 'available',
      activeResponseId: responseId, renderedEnergy: 1, playedPrefix: 1,
    })
    expect(controller.snapshot()).toMatchObject({
      response: 'idle', playback: 'idle', activeResponseId: null,
    })
  })

  test('容量超過で破棄された発話は文字起こし中表示を解除する', async () => {
    const { controller, coreEventReceivers } = setup()
    await controller.ensureSession({
      characterId: 'miori', conversationId: 'conversation-id',
    })
    await controller.resumeMicrophone()
    await controller.speechStopped(UTTERANCE_ID, 1_020)
    expect(controller.snapshot().input).toBe('transcribing')

    coreEventReceivers[0]({
      type: 'utterance_discarded',
      utterance_id: UTTERANCE_ID,
      reason: 'input_capacity_exceeded',
    } as VoiceSessionEvent)

    expect(controller.snapshot().input).toBe('listening')
    expect(controller.snapshot().response).toBe('idle')
  })

  test('再接続猶予を超えると音声sessionだけを終了してconversationを保持する', async () => {
    vi.useFakeTimers()
    try {
      const { controller, dependencies, observations } = setup()
      const context = { characterId: 'miori', conversationId: 'conversation-id' }
      await controller.ensureSession(context)

      observations[0]({
        transport: 'unavailable', control: 'unavailable', audio: 'unavailable',
      })
      await vi.advanceTimersByTimeAsync(60_000)

      expect(controller.snapshot()).toMatchObject({
        phase: 'ended', context, sessionId: null, input: 'inactive',
      })
      expect(dependencies.endSession).toHaveBeenCalledWith(SESSION_ID)
    } finally {
      vi.useRealTimers()
    }
  })

  test('再接続猶予切れのsession終了失敗を未処理rejectionにしない', async () => {
    vi.useFakeTimers()
    try {
      const { controller, dependencies, observations } = setup()
      vi.mocked(dependencies.endSession).mockRejectedValue(new Error('end failed'))
      await controller.ensureSession({
        characterId: 'miori', conversationId: 'conversation-id',
      })

      observations[0]({
        transport: 'unavailable', control: 'unavailable', audio: 'unavailable',
      })
      await vi.advanceTimersByTimeAsync(60_000)

      expect(controller.snapshot().phase).toBe('ended')
      expect(dependencies.endSession).toHaveBeenCalledWith(SESSION_ID)
    } finally {
      vi.useRealTimers()
    }
  })

  test('Backend終端後は古いRoomを再利用せず同じconversationで再開する', async () => {
    const { controller, coreEventReceivers, dependencies, room } = setup()
    const context = { characterId: 'miori', conversationId: 'conversation-id' }
    await controller.ensureSession(context)

    coreEventReceivers[0]({ type: 'session_ended' } as VoiceSessionEvent)

    expect(controller.snapshot()).toMatchObject({
      phase: 'ended', context, sessionId: null, input: 'inactive',
    })
    expect(room.disconnect).toHaveBeenCalledTimes(1)

    await controller.ensureSession(context)
    expect(dependencies.requestToken).toHaveBeenCalledTimes(2)
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
      phase: 'idle',
      input: 'inactive',
      response: 'idle',
      playback: 'idle',
      context: null,
      sessionId: null,
      activeResponseId: null,
    })
  })
})
