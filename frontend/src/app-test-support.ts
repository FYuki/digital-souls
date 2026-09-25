import { act, fireEvent, screen, waitFor } from '@testing-library/svelte'
import { expect, vi } from 'vitest'

export const CONVERSATION_ID = 'e98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
export const SECOND_CONVERSATION_ID = '6ad9a610-02cc-4a41-b02e-503826f7292b'
export const THIRD_CONVERSATION_ID = 'f98d6c65-1ae9-4d6f-a8c8-d59b0ad09010'
export const TURN_ID = '9e70795d-e5d5-431d-baa2-67f884403010'
export const VOICE_SESSION_ID = '20000000-0000-4000-8000-000000000010'
export const VOICE_PARTICIPANT_ID = '40000000-0000-4000-8000-000000000010'
export const RESPONSE_ID = '50000000-0000-4000-8000-000000000010'

export const conversation = {
  character_id: 'miori',
  conversation_id: CONVERSATION_ID,
  created_at: '2026-08-01T12:00:00+00:00',
  updated_at: '2026-08-01T12:01:00+00:00',
  archived_at: null,
  title: CONVERSATION_ID,
}

export const audioMocks = {
  pcmData: new ArrayBuffer(4),
  vadStart: vi.fn(),
  vadDestroy: vi.fn(),
  recorderInitialize: vi.fn(),
  recorderStart: vi.fn(),
  recorderStopAndTake: vi.fn(),
  recorderClose: vi.fn(),
  getUserMedia: vi.fn(),
  microphoneStream: { getTracks: () => [], getAudioTracks: () => [] } as unknown as MediaStream,
  vadOptions: undefined as
    | {
        onFrameProcessed: (probabilities: { isSpeech: number; notSpeech: number }, frame: Float32Array) => void
        onSpeechStart: () => void
        onSpeechRealStart: () => void
        onSpeechEnd: () => void
      }
    | undefined,
}

export const decodeAudioData = vi.fn()
export const createBufferSource = vi.fn()
export const connect = vi.fn()
export const start = vi.fn()
export const close = vi.fn()
export const fetchMock = vi.fn()

export const characterCatalog = [{
  character_id: 'miori',
  display_name: '光織',
  standing_image: {
    status: 'available',
    url: '/api/characters/miori/assets/standing/default.png',
  },
}]

export const uiSettings = {
  user_id: 'local',
  desktop_portrait_layout: 'right',
  desktop_history_height_percent: 75,
  compact_history_height_percent: 75,
  characters: [{
    character_id: 'miori',
    visible: true,
    pinned: false,
    pin_order: null,
  }],
  thread_pins: [],
}

export type CoreEventReceiver = (event: Record<string, unknown>) => void
export type RoomObserver = (event: Record<string, unknown>) => void
export const liveKitMocks = {
  connect: vi.fn(async () => undefined),
  publishMicrophone: vi.fn(async () => 'TR_initial'),
  muteMicrophone: vi.fn(async () => undefined),
  stopPlayback: vi.fn(() => 0),
  publishControlEvent: vi.fn(async (event: Record<string, unknown>) => {
    liveKitMocks.controlEvents.push(event)
  }),
  disconnect: vi.fn(),
  controlEvents: [] as Record<string, unknown>[],
  receiveCoreEvent: undefined as CoreEventReceiver | undefined,
  observeRoom: undefined as RoomObserver | undefined,
}

export class FakeAudioContext {
  destination = {}
  decodeAudioData = decodeAudioData
  createBufferSource = createBufferSource
  close = close
}

let turnSequence = 0

export const persistedTurn = (userContent: string, assistantContent: string) => ({
  kind: 'content',
  turn_id: `9e70795d-e5d5-431d-baa2-${String(++turnSequence).padStart(12, '0')}`,
  user_content: userContent,
  assistant_content: assistantContent,
})

export const defaultFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = String(input)
  if (url === '/api/characters') {
    return new Response(JSON.stringify(characterCatalog), { status: 200 })
  }
  if (url === '/api/characters/rescan') {
    return new Response(JSON.stringify(characterCatalog), { status: 200 })
  }
  if (url.startsWith('/api/ui-settings')) {
    return new Response(JSON.stringify(uiSettings), { status: 200 })
  }
  if (url === '/api/voice/livekit/token') {
    return new Response(JSON.stringify({
      session_id: VOICE_SESSION_ID,
      participant_id: VOICE_PARTICIPANT_ID,
      room: 'mock-room',
      token: 'mock-token',
      livekit_url: 'ws://mock-livekit.invalid',
      expires_at: '2026-08-28T12:00:00.000Z',
      reconnect_grace_ms: 60_000,
    }), { status: 200 })
  }
  if (url.startsWith('/api/voice/livekit/sessions/') && init?.method === 'DELETE') {
    return new Response(null, { status: 204 })
  }
  if (url === '/api/chat') {
    const body = JSON.parse(String(init?.body)) as Record<string, string>
    return new Response(JSON.stringify({
      character: body.character,
      turn: persistedTurn(body.message, 'HTTP応答です。'),
    }), { status: 200 })
  }
  if (url.endsWith('/turns')) {
    return new Response(JSON.stringify([
      persistedTurn('保存済みの質問', '保存済みの回答'),
    ]), { status: 200 })
  }
  if (url.endsWith('/archived')) return new Response('[]', { status: 200 })
  if (init?.method === 'POST') return new Response(JSON.stringify(conversation), { status: 200 })
  return new Response(JSON.stringify([conversation]), { status: 200 })
}

export const selectConversation = async (): Promise<void> => {
  await fireEvent.click(
    await screen.findByRole('button', { name: new RegExp(`^${CONVERSATION_ID}$`) }),
  )
}

export const chooseThreadAction = async (
  action: 'アーカイブ' | '復元' | '削除' | '名前を変更',
  title = CONVERSATION_ID,
): Promise<void> => {
  await fireEvent.click(await screen.findByRole('button', { name: `${title}のメニュー` }))
  await fireEvent.click(screen.getByRole('menuitem', { name: action }))
}

export const showArchived = async (): Promise<void> => {
  const button = await screen.findByRole<HTMLButtonElement>('button', {
    name: 'アーカイブ済み',
  })
  await waitFor(() => expect(button.disabled).toBe(false))
  await fireEvent.click(button)
}

export const showActive = async (): Promise<void> => {
  await fireEvent.click(await screen.findByRole('button', { name: '会話履歴に戻る' }))
}

export const startLiveKitSession = async () => {
  await selectConversation()
  await fireEvent.click(screen.getByRole('button', { name: 'マイクをオンにする' }))
  await waitFor(() => expect(liveKitMocks.publishMicrophone).toHaveBeenCalledTimes(1))
  await screen.findByRole('button', {name: 'マイクをオフにする'})
}

export const emitCoreEvent = async (event: Record<string, unknown>) => {
  const receiver = liveKitMocks.receiveCoreEvent
  if (receiver === undefined) throw new Error('LiveKit core event receiver is required')
  await act(() => receiver({ session_id: VOICE_SESSION_ID, ...event }))
}

// UI単体試験のBE通知fixture。実PCMからの境界検出はBackendの実VAD試験で検証する。
export const emitSpeech = async (type: 'speech_started' | 'speech_stopped') => {
  const request = liveKitMocks.controlEvents.findLast(event => event.type === 'audio_input_open_requested')
  if (!request) throw new Error('入力開始要求がない')
  await emitCoreEvent({
    protocol_version: '2.0', event_id: crypto.randomUUID(), monotonic_timestamp_ms: 1000,
    type, utterance_id: TURN_ID, speaker: {participant_id: VOICE_PARTICIPANT_ID, role: 'user'},
    track_sid: request.track_sid, input_generation: Number(String(request.track_sid).split('_').at(-1)),
    start_sample: 0, active_end_sample: 3200, detected_sample: 16000,
    sample_rate: 16000, clock_domain: 'server_monotonic',
  })
}

export const installAppTestGlobals = () => {
  localStorage.clear()
  window.history.replaceState({}, '', '/')
  turnSequence = 0
  audioMocks.vadOptions = undefined
  audioMocks.vadStart.mockReset().mockResolvedValue(undefined)
  audioMocks.vadDestroy.mockReset().mockResolvedValue(undefined)
  audioMocks.recorderInitialize.mockReset().mockResolvedValue(undefined)
  audioMocks.recorderStart.mockReset()
  audioMocks.recorderStopAndTake.mockReset().mockResolvedValue(audioMocks.pcmData)
  audioMocks.recorderClose.mockReset().mockResolvedValue(undefined)
  audioMocks.getUserMedia.mockReset().mockResolvedValue(audioMocks.microphoneStream)
  decodeAudioData.mockReset().mockResolvedValue({ duration: 1 })
  createBufferSource.mockReset().mockReturnValue({ connect, start })
  connect.mockReset()
  start.mockReset()
  close.mockReset().mockResolvedValue(undefined)
  liveKitMocks.connect.mockReset().mockResolvedValue(undefined)
  let microphoneIndex = 0
  liveKitMocks.publishMicrophone.mockReset().mockImplementation(async () => `TR_test_${++microphoneIndex}`)
  liveKitMocks.muteMicrophone.mockReset().mockResolvedValue(undefined)
  liveKitMocks.stopPlayback.mockReset().mockReturnValue(0)
  liveKitMocks.publishControlEvent.mockReset().mockImplementation(
    async (event: Record<string, unknown>) => {
      liveKitMocks.controlEvents.push(event)
      if (event.type === 'audio_input_open_requested') {
        liveKitMocks.receiveCoreEvent?.({
          protocol_version: '2.0', event_id: crypto.randomUUID(),
          session_id: VOICE_SESSION_ID, monotonic_timestamp_ms: 1000,
          type: 'audio_input_opened', request_event_id: event.event_id,
          track_sid: event.track_sid, input_revision: event.input_revision,
          input_generation: microphoneIndex,
        })
      }
    },
  )
  liveKitMocks.disconnect.mockReset()
  liveKitMocks.controlEvents = []
  liveKitMocks.receiveCoreEvent = undefined
  liveKitMocks.observeRoom = undefined
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => TURN_ID) })
  fetchMock.mockReset().mockImplementation(async (input, init) => defaultFetch(input, init))
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('AudioContext', FakeAudioContext)
  vi.stubGlobal('navigator', {
    mediaDevices: { getUserMedia: audioMocks.getUserMedia },
  })
  vi.stubGlobal('__digitalSoulsVoiceSessionTestPort', {
    createRoom: (observe: RoomObserver, receiveCoreEvent: CoreEventReceiver) => {
      liveKitMocks.observeRoom = observe
      liveKitMocks.receiveCoreEvent = receiveCoreEvent
      return {
        connect: liveKitMocks.connect,
        publishMicrophone: liveKitMocks.publishMicrophone,
        muteMicrophone: liveKitMocks.muteMicrophone,
        stopPlayback: liveKitMocks.stopPlayback,
        publishControlEvent: liveKitMocks.publishControlEvent,
        disconnect: liveKitMocks.disconnect,
      }
    },
  })
}

export const cleanupAppTestGlobals = () => {
  vi.unstubAllGlobals()
}
