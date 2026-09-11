import { afterEach, describe, expect, test, vi } from 'vitest'

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
const MICROPHONE_STREAM = {getAudioTracks: () => []} as unknown as MediaStream

afterEach(() => { vi.useRealTimers() })

const setup = () => {
  const events: VoiceSessionEvent[] = []
  const snapshots: VoiceSessionSnapshot[] = []
  const observations: Array<(value: RoomObservation) => void> = []
  const coreEventReceivers: Array<(event: VoiceSessionEvent) => void> = []
  const delivered: Array<{event: VoiceSessionEvent; context: {characterId: string; conversationId: string}}> = []
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
    (event, context) => delivered.push({event, context}),
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
    delivered,
  }
}

describe('通常会話UI向けLiveKit音声session', () => {
  test('text submitで即時停止し、playback通知ACKを待たずcancelとtextを送り旧deltaを捨てる', async () => {
    const {controller, room, events, coreEventReceivers, observations, delivered} = setup()
    const context = {characterId: 'miori', conversationId: 'a'}
    const oldResponse = '50000000-0000-4000-8000-000000000051'
    const newResponse = '50000000-0000-4000-8000-000000000052'
    await controller.ensureSession(context)
    coreEventReceivers[0]({type: 'response_started', response_id: oldResponse} as VoiceSessionEvent)
    let release: () => void = () => undefined
    vi.mocked(room.publishControlEvent).mockImplementation(async event => {
      events.push(event)
      if (event.type === 'playback_stopped') await new Promise<void>(resolve => {release = resolve})
    })
    const submitted = controller.submitText(context, '新しい質問')
    expect(room.stopPlayback).toHaveBeenCalledWith(oldResponse)
    await submitted
    expect(events.map(event => event.type)).toEqual(expect.arrayContaining(['response_cancel_requested', 'user_text_submitted']))
    expect(controller.snapshot().response).toBe('interrupting')
    coreEventReceivers[0]({type: 'response_delta', response_id: oldResponse, text: '遅着'} as VoiceSessionEvent)
    expect(controller.snapshot().response).toBe('interrupting')
    coreEventReceivers[0]({type: 'response_started', response_id: newResponse} as VoiceSessionEvent)
    coreEventReceivers[0]({type: 'response_delta', response_id: oldResponse, text: 'さらに遅着'} as VoiceSessionEvent)
    observations[0]({transport: 'available', control: 'available', audio: 'available', activeResponseId: oldResponse, renderedEnergy: 1})
    expect(controller.snapshot()).toMatchObject({activeResponseId: newResponse, playback: 'idle'})
    expect(delivered.some(row => row.event.type === 'response_delta')).toBe(false)
    release()
    await controller.end()
  })

  test('focusだけで音声入力を抑止し、回答と接続を保持してblurで再開する', async () => {
    const {controller, room, events, coreEventReceivers} = setup()
    const context = {characterId: 'miori', conversationId: 'a'}
    const track = {enabled: true}
    const stream = {getAudioTracks: () => [track]} as unknown as MediaStream
    await controller.ensureSession(context)
    await controller.resumeMicrophone(stream)
    coreEventReceivers[0]({type: 'response_started', response_id: 'response-a'} as VoiceSessionEvent)
    const focus = controller.setTextInputFocused(context, true)
    expect(track.enabled).toBe(false)
    await focus
    await controller.speechStarted(UTTERANCE_ID, 1010)
    await controller.speechStopped(UTTERANCE_ID, 1020)
    expect(controller.snapshot()).toMatchObject({input: 'suppressed', response: 'generating'})
    expect(events.some(event => event.type === 'speech_started' || event.type === 'session_muted')).toBe(false)
    expect(room.stopPlayback).not.toHaveBeenCalled()
    expect(room.disconnect).not.toHaveBeenCalled()
    await controller.setTextInputFocused(context, false)
    expect(track.enabled).toBe(true)
    expect(controller.snapshot().input).toBe('listening')
    expect(events.filter(event => event.type === 'audio_input_suppression_changed').map(event => event.suppressed)).toEqual([true, false])
  })

  test.each(['manual', 'thread_switch'] as const)('%s muteはfocus解除や別スレッドの操作で解除しない', async reason => {
    const {controller, room} = setup()
    const context = {characterId: 'miori', conversationId: 'a'}
    await controller.ensureSession(context)
    await controller.resumeMicrophone(MICROPHONE_STREAM)
    if (reason === 'manual') await controller.muteMicrophone()
    else await controller.muteForThreadSwitch()
    await controller.setTextInputFocused(context, true)
    await controller.setTextInputFocused({characterId: 'miori', conversationId: 'b'}, false)
    await controller.setTextInputFocused(context, false)
    expect(controller.snapshot().input).toBe('muted')
    expect(room.publishMicrophone).toHaveBeenCalledTimes(1)
    await controller.resumeMicrophone(MICROPHONE_STREAM)
    expect(controller.snapshot().input).toBe('listening')
  })

  test('連続focus変更は最新の抑止ACKまで音声を再開しない', async () => {
    const {controller, room} = setup()
    const context = {characterId: 'miori', conversationId: 'a'}
    const track = {enabled: true}
    await controller.ensureSession(context)
    await controller.resumeMicrophone({getAudioTracks: () => [track]} as unknown as MediaStream)
    let acknowledge: () => void = () => undefined
    vi.mocked(room.publishControlEvent).mockImplementationOnce(() => new Promise<void>(resolve => {acknowledge = resolve}))
    const focused = controller.setTextInputFocused(context, true)
    const blurred = controller.setTextInputFocused(context, false)
    const refocused = controller.setTextInputFocused(context, true)
    await vi.waitFor(() => expect(room.publishControlEvent).toHaveBeenCalledTimes(3))
    expect(track.enabled).toBe(false)
    acknowledge()
    await Promise.all([focused, blurred, refocused])
    expect(track.enabled).toBe(false)
    expect(controller.snapshot().input).toBe('suppressed')
  })

  test('再接続中のfocus解除を復旧時に照合し、手動muteは保持する', async () => {
    const {controller, observations, events} = setup()
    const context = {characterId: 'miori', conversationId: 'a'}
    await controller.ensureSession(context)
    await controller.resumeMicrophone(MICROPHONE_STREAM)
    await controller.setTextInputFocused(context, true)
    observations[0]({transport: 'unavailable', control: 'unavailable', audio: 'unavailable'})
    await controller.setTextInputFocused(context, false)
    await controller.muteMicrophone()
    observations[0]({transport: 'available', control: 'available', audio: 'available'})
    await vi.waitFor(() => expect(events.filter(event => event.type === 'audio_input_suppression_changed').at(-1)).toMatchObject({suppressed: false}))
    expect(controller.snapshot().input).toBe('muted')
  })

  test('sessionとutteranceを分離し、mute後も同じsessionを再開する', async () => {
    const { controller, dependencies, events, room } = setup()
    const context = { characterId: 'miori', conversationId: 'conversation-id' }

    await controller.ensureSession(context)
    await controller.resumeMicrophone(MICROPHONE_STREAM)
    await controller.speechStarted(UTTERANCE_ID, 1_010)
    await controller.speechStopped(UTTERANCE_ID, 1_020)
    await controller.muteMicrophone()
    await controller.resumeMicrophone(MICROPHONE_STREAM)

    expect(dependencies.requestToken).toHaveBeenCalledTimes(1)
    expect(room.publishMicrophone).toHaveBeenCalledTimes(2)
    expect(room.muteMicrophone).toHaveBeenCalledTimes(1)
    expect(events.map((event) => event.type)).toEqual([
      'session_start_requested',
      'session_resumed',
      'speech_started',
      'speech_stopped',
      'observation',
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
    await controller.resumeMicrophone(MICROPHONE_STREAM)

    observations[0]({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
    observations[0]({ transport: 'available', control: 'available', audio: 'available' })

    expect(controller.snapshot().phase).toBe('listening')
  })

  test('再接続中の明示muteは操作可能へ戻さず、復旧後もmutedを維持する', async () => {
    const {controller, observations} = setup()
    await controller.ensureSession({characterId: 'miori', conversationId: 'conversation-id'})
    await controller.resumeMicrophone(MICROPHONE_STREAM)
    observations[0]({transport: 'unavailable', control: 'unavailable', audio: 'unavailable'})
    await controller.muteMicrophone()
    expect(controller.snapshot().phase).toBe('reconnecting')
    observations[0]({transport: 'available', control: 'available', audio: 'unavailable'})
    expect(controller.snapshot().phase).toBe('muted')
  })

  test('前sessionのmicrophone状態を次sessionへ持ち越さない', async () => {
    const { controller, observations } = setup()
    await controller.ensureSession({ characterId: 'miori', conversationId: 'one' })
    await controller.resumeMicrophone(MICROPHONE_STREAM)
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

    await expect(controller.resumeMicrophone(MICROPHONE_STREAM)).rejects.toThrow(
      'permission or publish failure',
    )
    expect(controller.snapshot().phase).toBe('muted')

    await controller.resumeMicrophone(MICROPHONE_STREAM)
    expect(controller.snapshot().phase).toBe('listening')
    expect(room.publishMicrophone).toHaveBeenCalledTimes(2)
  })

  test('生成・再生中もspeech startでは継続しtake turn判定後だけ停止する', async () => {
    const { controller, coreEventReceivers, events, observations, room } = setup()
    await controller.ensureSession({
      characterId: 'miori', conversationId: 'conversation-id',
    })
    await controller.resumeMicrophone(MICROPHONE_STREAM)
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

    expect(room.stopPlayback).not.toHaveBeenCalled()
    expect(events.slice(2).map((event) => event.type)).toEqual([
      'speech_started',
      'speech_started',
    ])
    expect(events.at(-1)).toMatchObject({
      type: 'speech_started',
      response_id: '50000000-0000-4000-8000-000000000001',
    })

    coreEventReceivers[0]({
      type: 'turn_decision',
      utterance_id: UTTERANCE_ID,
      response_id: '50000000-0000-4000-8000-000000000001',
      decision: 'take_turn',
      final: false,
    } as VoiceSessionEvent)
    coreEventReceivers[0]({
      type: 'turn_decision',
      utterance_id: UTTERANCE_ID,
      response_id: '50000000-0000-4000-8000-000000000001',
      decision: 'take_turn',
      final: true,
    } as VoiceSessionEvent)
    await Promise.resolve()

    expect(room.stopPlayback).toHaveBeenCalledTimes(1)
    expect(room.stopPlayback).toHaveBeenCalledWith(
      '50000000-0000-4000-8000-000000000001', 1_010,
    )
    await vi.waitFor(() => expect(events.map((event) => event.type)).toContain('playback_stopped'))
    expect(events.map((event) => event.type)).not.toContain('response_cancel_requested')
    expect(controller.snapshot()).toMatchObject({
      response: 'interrupting', playback: 'stopped',
    })
  })

  test('生成完了後の相槌では再生と履歴完成状態を維持する', async () => {
    const { controller, coreEventReceivers, events, observations, room } = setup()
    await controller.ensureSession({
      characterId: 'miori', conversationId: 'conversation-id',
    })
    await controller.resumeMicrophone(MICROPHONE_STREAM)
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

    coreEventReceivers[0]({
      type: 'turn_decision',
      utterance_id: UTTERANCE_ID,
      response_id: '50000000-0000-4000-8000-000000000001',
      decision: 'backchannel',
      final: true,
    } as VoiceSessionEvent)

    expect(room.stopPlayback).not.toHaveBeenCalled()
    expect(events.map((event) => event.type)).toEqual([
      'session_start_requested', 'session_resumed', 'speech_started',
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
    expect(controller.snapshot()).toMatchObject({playback: 'playing'})
    observations[0]({transport: 'available', control: 'available', audio: 'available',
      activeResponseId: responseId, playbackCompletedResponseId: responseId})
    expect(controller.snapshot()).toMatchObject({
      response: 'idle', playback: 'idle', activeResponseId: null,
    })
  })

  test('容量超過で破棄された発話は文字起こし中表示を解除する', async () => {
    const { controller, coreEventReceivers } = setup()
    await controller.ensureSession({
      characterId: 'miori', conversationId: 'conversation-id',
    })
    await controller.resumeMicrophone(MICROPHONE_STREAM)
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
      textSubmissions: [],
    })
  })
})


describe('音声sessionのテキスト受付と結果照合', () => {
  const context = {characterId: 'miori', conversationId: 'thread-a'}
  const result = (inputId: string, status: 'processing' | 'accepted' | 'rejected' | 'not_received',
    sessionId = SESSION_ID): VoiceSessionEvent => ({
    type: 'user_input_result', protocol_version: '1.1', session_id: sessionId,
    event_id: crypto.randomUUID(), monotonic_timestamp_ms: 1, input_event_id: inputId, status,
  })

  test('配送完了で本文を消さず、BE受理結果を送信元スレッドへ対応付ける', async () => {
    const {controller, events, coreEventReceivers} = setup()
    await controller.ensureSession(context)
    const id = await controller.submitText(context, '直接テキスト')
    expect(controller.snapshot().textSubmissions).toEqual([{
      inputId: id, sessionId: SESSION_ID, context, text: '直接テキスト',
      status: 'sending', responseId: null, errorCode: null,
    }])
    expect(controller.snapshot().response).toBe('idle')
    expect(controller.canSubmitText(context)).toBe(false)
    coreEventReceivers[0](result(id, 'accepted'))
    expect(controller.snapshot().textSubmissions[0].status).toBe('accepted')
    expect(controller.canSubmitText(context)).toBe(true)
    coreEventReceivers[0](result(id, 'processing'))
    expect(controller.snapshot().textSubmissions[0].status).toBe('accepted')
    expect(events.filter(event => event.type === 'user_text_submitted')).toHaveLength(1)
    await controller.end()
  })

  test('表示スレッドが異なる送信で既存sessionを誤操作しない', async () => {
    const {controller, room, events, dependencies} = setup()
    await controller.ensureSession(context)
    const before = events.length
    await expect(controller.submitText({...context, conversationId: 'thread-b'}, '別スレッド')).rejects.toThrow()
    expect(events).toHaveLength(before)
    expect(room.stopPlayback).not.toHaveBeenCalled()
    expect(dependencies.endSession).not.toHaveBeenCalled()
    await controller.end()
  })

  test('切断後は元のIDを照合し、新しい入力として自動再送しない', async () => {
    const {controller, observations, events, coreEventReceivers} = setup()
    await controller.ensureSession(context)
    const id = await controller.submitText(context, '送信確認を続ける本文')
    observations[0]({transport: 'unavailable', control: 'unavailable', audio: 'unavailable'})
    expect(controller.snapshot().textSubmissions[0]).toMatchObject({status: 'confirming', text: '送信確認を続ける本文'})
    observations[0]({transport: 'available', control: 'available', audio: 'available'})
    await Promise.resolve()
    expect(events.filter(event => event.type === 'user_input_result_requested')).toMatchObject([{input_event_id: id}])
    expect(events.filter(event => event.type === 'user_text_submitted')).toHaveLength(1)
    coreEventReceivers[0](result(id, 'accepted'))
    await controller.end()
  })

  test.each(['not_received', 'rejected'] as const)('BEの%sだけが明示的な再送を許可する', async status => {
    const {controller, coreEventReceivers} = setup()
    await controller.ensureSession(context)
    const id = await controller.submitText(context, '再送する本文')
    coreEventReceivers[0](result(id, status, 'other-session'))
    expect(controller.canSubmitText(context)).toBe(false)
    coreEventReceivers[0](result(id, status))
    const nextId = await controller.submitText(context, '再送する本文')
    expect(nextId).not.toBe(id)
    coreEventReceivers[0](result(nextId, 'accepted'))
    await controller.end()
  })

  test('publish例外とsession終了を未受理へ読み替えず本文を保持する', async () => {
    const {controller, room} = setup()
    await controller.ensureSession(context)
    vi.mocked(room.publishControlEvent).mockRejectedValueOnce(new Error('connection lost'))
    await controller.submitText(context, '失われてはいけない本文')
    expect(controller.snapshot().textSubmissions[0].status).toBe('confirming')
    await controller.end()
    expect(controller.snapshot().textSubmissions[0]).toMatchObject({status: 'confirming', text: '失われてはいけない本文', context})
  })

  test('結果未着のタイムアウトで照合を行い、processingの間は新規送信しない', async () => {
    vi.useFakeTimers()
    const {controller, coreEventReceivers, events} = setup()
    await controller.ensureSession(context)
    const id = await controller.submitText(context, '結果通知待ち')
    await vi.advanceTimersByTimeAsync(5_000)
    expect(controller.snapshot().textSubmissions[0].status).toBe('confirming')
    coreEventReceivers[0](result(id, 'processing'))
    expect(controller.canSubmitText(context)).toBe(false)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(events.filter(event => event.type === 'user_input_result_requested')).toHaveLength(2)
    coreEventReceivers[0](result(id, 'accepted'))
    await vi.advanceTimersByTimeAsync(10_000)
    expect(events.filter(event => event.type === 'user_input_result_requested')).toHaveLength(2)
    await controller.end()
  })

  test('元publishが完了待ちでも再接続照合を送信できる', async () => {
    const {controller, room, observations, events, coreEventReceivers} = setup()
    await controller.ensureSession(context)
    let release: () => void = () => undefined
    vi.mocked(room.publishControlEvent).mockImplementationOnce(async event => {
      events.push(event)
      await new Promise<void>(resolve => { release = resolve })
    })
    const submitted = controller.submitText(context, 'publish待ちの入力')
    const id = controller.snapshot().textSubmissions[0].inputId
    observations[0]({transport: 'unavailable', control: 'unavailable', audio: 'unavailable'})
    observations[0]({transport: 'available', control: 'available', audio: 'available'})
    await Promise.resolve()
    expect(events.filter(event => event.type === 'user_input_result_requested')).toHaveLength(1)
    coreEventReceivers[0](result(id, 'accepted'))
    release()
    await submitted
    await controller.end()
  })

  test('回答eventは表示先を参照せずsessionの所属スレッドと共に通知する', async () => {
    const {controller, delivered, coreEventReceivers} = setup()
    await controller.ensureSession(context)
    coreEventReceivers[0]({type: 'response_started', response_id: 'response-a'} as VoiceSessionEvent)
    expect(delivered[0].context).toEqual(context)
    expect(controller.snapshot().response).toBe('generating')
    await controller.end()
  })

  test('明示中断は即時local stopを行い、BE取消成立を先取りしない', async () => {
    const {controller, room, events, coreEventReceivers} = setup()
    await controller.ensureSession(context)
    const responseId = '50000000-0000-4000-8000-000000000001'
    coreEventReceivers[0]({type: 'response_started', response_id: responseId} as VoiceSessionEvent)
    const interrupt = controller.interruptResponse(context)
    expect(room.stopPlayback).toHaveBeenCalledWith(responseId)
    expect(controller.snapshot().response).toBe('interrupting')
    await interrupt
    expect(events.slice(-2).map(event => event.type)).toEqual(['playback_stopped', 'response_cancel_requested'])
    expect(controller.snapshot().response).toBe('interrupting')
    coreEventReceivers[0]({type: 'response_cancelled', response_id: responseId} as VoiceSessionEvent)
    expect(controller.snapshot().response).toBe('idle')
    await controller.end()
  })
})

describe('session単位の操作計測', () => {
  const summaries = (events: VoiceSessionEvent[]) => events
    .filter(event => event.type === 'observation' && event.measurement === 'session_summary')
    .map(event => event.session_summary!)

  test('getUserMedia前の開始試行を保持し自動再接続を操作へ加算しない', async () => {
    const { controller, events, observations } = setup()
    await controller.ensureSession({ characterId: 'miori', conversationId: 'conversation-id' })
    controller.recordMicrophoneActivationAttempt()
    // 1回目はgetUserMedia失敗を想定。公開完了の通知は来ない。
    controller.recordMicrophoneActivationAttempt()
    await controller.resumeMicrophone(MICROPHONE_STREAM)
    observations[0]({ transport: 'unavailable', control: 'unavailable', audio: 'unavailable' })
    observations[0]({ transport: 'available', control: 'available', audio: 'available' })
    await controller.end()
    expect(summaries(events).at(-1)).toMatchObject({
      microphone_activation_attempts: 2, mute_attempts: 0, retry_attempts: 0,
      operation_tracking_started: true, end_requested: true,
    })
  })

  test('失敗したmuteを最終summaryに残し、終了通知を切断より先に送る', async () => {
    const { controller, events, room } = setup()
    await controller.ensureSession({ characterId: 'miori', conversationId: 'conversation-id' })
    controller.recordMicrophoneActivationAttempt()
    vi.mocked(room.muteMicrophone).mockRejectedValue(new Error('mute failed'))
    await expect(controller.muteMicrophone()).rejects.toThrow('mute failed')
    vi.mocked(room.disconnect).mockImplementation(() => {
      expect(summaries(events).at(-1)?.end_requested).toBe(true)
    })
    await controller.end()
    expect(summaries(events).at(-1)?.mute_attempts).toBe(1)
  })

  test('接続前の手動再試行を新sessionに保持する', async () => {
    const { controller, dependencies, events } = setup()
    vi.mocked(dependencies.requestToken).mockRejectedValueOnce(new Error('token failed'))
    controller.recordRetryAttempt()
    await expect(controller.ensureSession({ characterId: 'miori', conversationId: 'conversation-id' })).rejects.toThrow()
    controller.recordRetryAttempt()
    await controller.ensureSession({ characterId: 'miori', conversationId: 'conversation-id' })
    controller.recordMicrophoneActivationAttempt()
    await controller.end()
    expect(summaries(events).at(-1)?.retry_attempts).toBe(2)
  })

  test('summary送信が停止しても終了APIと切断を500ms以内に実行する', async () => {
    vi.useFakeTimers()
    try {
      const { controller, room, dependencies } = setup()
      await controller.ensureSession({ characterId: 'miori', conversationId: 'conversation-id' })
      vi.mocked(room.publishControlEvent).mockImplementation(() => new Promise(() => undefined))
      const ended = controller.end()
      await vi.advanceTimersByTimeAsync(500)
      await ended
      expect(room.disconnect).toHaveBeenCalledTimes(1)
      expect(dependencies.endSession).toHaveBeenCalledWith(SESSION_ID)
      expect(vi.getTimerCount()).toBe(0)
    } finally { vi.useRealTimers() }
  })
})
