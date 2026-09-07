import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

const livekitMocks = vi.hoisted(() => {
  class FakeRoom {
    readonly handlers = new Map<string, Array<(...args: unknown[]) => void>>()
    readonly localParticipant = {
      publishData: vi.fn(async (_payload: Uint8Array, _options: unknown) => undefined),
      publishTrack: vi.fn(async () => undefined),
      unpublishTrack: vi.fn(async () => undefined),
      getTrackPublication: vi.fn(
        (_source: unknown): { track?: unknown } | undefined => undefined,
      ),
      setMicrophoneEnabled: vi.fn(async () => undefined),
    }

    on(event: string, callback: (...args: unknown[]) => void): this {
      const handlers = this.handlers.get(event) ?? []
      handlers.push(callback)
      this.handlers.set(event, handlers)
      return this
    }

    emit(event: string, ...args: unknown[]): void {
      for (const callback of this.handlers.get(event) ?? []) callback(...args)
    }

    async connect(): Promise<void> {}

    disconnect(): void {
      this.emit('disconnected')
    }
  }

  return { FakeRoom, rooms: [] as FakeRoom[] }
})

vi.mock('livekit-client', () => ({
  Room: class extends livekitMocks.FakeRoom {
    constructor() {
      super()
      livekitMocks.rooms.push(this)
    }
  },
  RoomEvent: {
    Reconnecting: 'reconnecting',
    Reconnected: 'reconnected',
    DataReceived: 'dataReceived',
    TrackSubscribed: 'trackSubscribed',
    TrackUnsubscribed: 'trackUnsubscribed',
    Disconnected: 'disconnected',
  },
  Track: {
    Kind: { Audio: 'audio' },
    Source: { Microphone: 'microphone' },
  },
}))

const mediaMocks = vi.hoisted(() => ({observers: [] as Array<{
  playback?: {packet: (packet: unknown) => void; failed: () => void; interrupted?: () => void}
  report: (value: unknown) => void
}>}))
vi.mock('./livekit/media-observer', () => ({
  RemoteMediaObserver: class {
    constructor(_receiver: unknown, _track: unknown, public report: (value: unknown) => void,
      public playback?: {packet: (packet: unknown) => void; failed: () => void; interrupted?: () => void}) {mediaMocks.observers.push(this); report({trackReceivedAtMs: performance.now()})}
    ready = async () => undefined
    close = vi.fn()
  },
}))

import { LiveKitRoomClient, type RoomObservation } from './livekit/room'

type Deferred = Readonly<{
  promise: Promise<void>
  resolve: () => void
}>

const deferred = (): Deferred => {
  let resolvePromise: (() => void) | undefined
  const promise = new Promise<void>((resolve) => {
    resolvePromise = resolve
  })
  return {
    promise,
    resolve: () => resolvePromise?.(),
  }
}

const audioContexts: FakeAudioContext[] = []
const closeBlockers: Deferred[] = []
const workletFailures: Error[] = []
const workletBlockers: Deferred[] = []

class FakeAudioWorkletNode {
  readonly port = {onmessage: null as ((event: MessageEvent) => void) | null, postMessage: vi.fn(), close: vi.fn()}
  disconnect = vi.fn()
  connect = vi.fn((destination: unknown) => destination)
  constructor(context: FakeAudioContext) {context.worklets.push(this)}
}

class FakeGainNode {
  readonly gain = { value: 1 }
  disconnect = vi.fn()

  connect(destination: unknown): unknown {
    return destination
  }
}

class FakeAudioContext {
  readonly destination = {}
  readonly sampleRate = 48_000
  readonly currentTime = 0
  readonly audioWorklet = { addModule: vi.fn(async () => {
    const failure = workletFailures.shift()
    if (failure !== undefined) throw failure
    await workletBlockers.shift()?.promise
  }) }
  readonly worklets: FakeAudioWorkletNode[] = []
  readonly gains: FakeGainNode[] = []

  constructor() {
    audioContexts.push(this)
  }

  getOutputTimestamp(): AudioTimestamp { return { contextTime: 1, performanceTime: 1000 } }

  async resume(): Promise<void> {}

  readonly close = vi.fn((): Promise<void> => {
    return closeBlockers.shift()?.promise ?? Promise.resolve()
  })

  createMediaStreamSource(): never {throw new Error('native PCM source must not be connected')}

  createGain(): FakeGainNode {
    const gain = new FakeGainNode()
    this.gains.push(gain)
    return gain
  }
}

const authoritativeState = (
  generation: number,
  terminalOutcomes: ReadonlyArray<Record<string, unknown>> = [],
): Uint8Array => new TextEncoder().encode(JSON.stringify({
  protocol_version: '1.0',
  type: 'authoritative_state',
  generation,
  session_phase: 'available',
  terminal_outcomes: terminalOutcomes,
}))

const latestRoom = (): InstanceType<typeof livekitMocks.FakeRoom> => {
  const room = livekitMocks.rooms.at(-1)
  if (room === undefined) throw new Error('LiveKit Room is required')
  return room
}

const emitPrivateFrame = (room: InstanceType<typeof livekitMocks.FakeRoom>, payload: Uint8Array) => {
  room.emit('dataReceived', payload, undefined, undefined, 'digital-souls.livekit-transport.v1')
}

const emitCoreEvent = (
  room: InstanceType<typeof livekitMocks.FakeRoom>,
  event: Record<string, unknown>,
) => {
  room.emit(
    'dataReceived',
    new TextEncoder().encode(JSON.stringify(event)),
    undefined,
    undefined,
    'digital-souls.core.v1',
  )
}

const emitScreenEvent = (
  room: InstanceType<typeof livekitMocks.FakeRoom>,
  event: Record<string, unknown>,
) => {
  room.emit(
    'dataReceived',
    new TextEncoder().encode(JSON.stringify(event)),
    undefined,
    undefined,
    'digital-souls.screen-perception.v1',
  )
}

describe('LiveKit Room generation synchronization', () => {
  beforeEach(() => {
    livekitMocks.rooms.length = 0
    audioContexts.length = 0
    closeBlockers.length = 0
    mediaMocks.observers.length = 0
    workletFailures.length = 0
    workletBlockers.length = 0
    vi.stubGlobal('AudioContext', FakeAudioContext)
    vi.stubGlobal('AudioWorkletNode', FakeAudioWorkletNode)
    vi.stubGlobal('MediaStream', class {
      constructor(_tracks: unknown[]) {}
    })
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:livekit-worklet'),
      revokeObjectURL: vi.fn(),
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  test.each([false, true])('再生worklet準備前・購読解除後はreadyを送らない（解除=%s）', async unsubscribe => {
    const client = new LiveKitRoomClient(() => undefined)
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()
    const blocker = deferred()
    workletBlockers.push(blocker)
    const publication = {trackSid: 'TR_ready', trackName: 'ds-response-v1:50000000-0000-4000-8000-000000000001'}
    const track = {kind: 'audio', mediaStreamTrack: {}}
    const readyFrames = () => room.localParticipant.publishData.mock.calls
      .map(([payload]) => JSON.parse(new TextDecoder().decode(payload)))
      .filter(frame => frame.type === 'response_track_ready')
    room.emit('trackSubscribed', track, publication, {})
    await vi.waitFor(() => expect(audioContexts).toHaveLength(1))
    expect(readyFrames()).toEqual([])
    if (unsubscribe) room.emit('trackUnsubscribed', track, publication, {})
    blocker.resolve()
    if (unsubscribe) {
      await new Promise(resolve => setTimeout(resolve, 0))
      expect(readyFrames()).toEqual([])
    } else {
      await vi.waitFor(() => expect(readyFrames()).toEqual([{
        protocol_version: '1.0', type: 'response_track_ready', response_id: '50000000-0000-4000-8000-000000000001',
        track_sid: 'TR_ready', generation: 0,
      }]))
    }
    client.disconnect()
  })

  test('ready送信の完了が切断後に戻っても音声利用可能へ戻さない', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient(value => observations.push(value))
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()
    const blocker = deferred()
    let readySent = false
    room.localParticipant.publishData.mockImplementation(async payload => {
      if (JSON.parse(new TextDecoder().decode(payload)).type === 'response_track_ready') {
        readySent = true
        await blocker.promise
      }
    })
    room.emit('trackSubscribed', {kind: 'audio', mediaStreamTrack: {}}, {
      trackSid: 'TR_ready', trackName: 'ds-response-v1:50000000-0000-4000-8000-000000000001',
    }, {})
    await vi.waitFor(() => expect(readySent).toBe(true))
    client.disconnect()
    blocker.resolve()
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(observations.at(-1)).toMatchObject({transport: 'idle', audio: 'unavailable'})
  })

  test('世代変更時に前世代の終端応答とactive responseを消去する', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient((observation) => observations.push(observation))
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()
    emitPrivateFrame(room, authoritativeState(0, [{
      type: 'response_interrupted',
      session_id: '20000000-0000-4000-8000-000000000001',
      response_id: '30000000-0000-4000-8000-000000000001',
      confirmed_audio_sequence: 4,
    }]))
    emitPrivateFrame(room, authoritativeState(1))

    const latest = observations.at(-1)
    expect(latest).toMatchObject({
      generation: 1,
      terminalResponseId: '',
      terminalConfirmedAudioSequence: 0,
      activeResponseId: '',
    })
    client.disconnect()
  })

  test('microphone publishとmuteでbrowser音声処理設定を維持する', async () => {
    const client = new LiveKitRoomClient(() => undefined)
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()

    await client.publishMicrophone()
    await client.muteMicrophone()

    expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenNthCalledWith(
      1,
      true,
      {
        echoCancellation: true,
        noiseSuppression: true,
        channelCount: 1,
      },
    )
    expect(room.localParticipant.setMicrophoneEnabled).toHaveBeenNthCalledWith(2, false)
  })

  test('VADと共有するmicrophone trackをLiveKitへpublishして明示的に解除する', async () => {
    const client = new LiveKitRoomClient(() => undefined)
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()
    const audioTrack = { kind: 'audio' } as MediaStreamTrack
    const localTrack = { kind: 'audio' }
    const stream = { getAudioTracks: () => [audioTrack] } as unknown as MediaStream
    room.localParticipant.getTrackPublication.mockReturnValue({ track: localTrack })

    await client.publishMicrophone(stream)
    await client.muteMicrophone()

    expect(room.localParticipant.publishTrack).toHaveBeenCalledWith(audioTrack, {
      source: 'microphone',
    })
    expect(room.localParticipant.unpublishTrack).toHaveBeenCalledWith(localTrack, false)
    expect(room.localParticipant.setMicrophoneEnabled).not.toHaveBeenCalled()
  })

  test('同一sessionへの明示的な再接続時に状態同期を要求する', async () => {
    const client = new LiveKitRoomClient(() => undefined)
    await client.connect('ws://127.0.0.1:7880', 'token-1', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()

    client.temporaryDisconnect()
    await client.connect('ws://127.0.0.1:7880', 'token-2', '20000000-0000-4000-8000-000000000001')

    expect(room.localParticipant.publishData).toHaveBeenCalledTimes(1)
    const [payload, options] = room.localParticipant.publishData.mock.calls[0]
    expect(JSON.parse(new TextDecoder().decode(payload as Uint8Array))).toEqual({
      protocol_version: '1.0',
      type: 'state_sync_request',
      generation: 0,
    })
    expect(options).toEqual({
      reliable: true,
      topic: 'digital-souls.livekit-transport.v1',
    })
  })

  test('連続する世代変更では古い音声graph再構築を再開しない', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient((observation) => observations.push(observation))
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()
    room.emit(
      'trackSubscribed',
      { kind: 'audio', mediaStreamTrack: {} },
      { trackSid: 'TR_audio', trackName: 'ds-response-v1:50000000-0000-4000-8000-000000000001' },
      {},
    )
    await vi.waitFor(() => {
      expect(audioContexts).toHaveLength(1)
      expect(observations.some((item) => item.activeAudioGraphs === 1)).toBe(true)
    })

    const firstClose = deferred()
    closeBlockers.push(firstClose)
    emitPrivateFrame(room, authoritativeState(1))
    emitPrivateFrame(room, authoritativeState(2))
    expect(audioContexts).toHaveLength(1)

    firstClose.resolve()
    await vi.waitFor(() => {
      expect(audioContexts).toHaveLength(2)
      expect(audioContexts.reduce((total, context) => total + context.worklets.length, 0)).toBe(2)
      expect(observations.at(-1)).toMatchObject({ activeAudioGraphs: 1 })
    })
    client.disconnect()
  })

  test('重複trackを二重再生せずunsubscribe後の同一track再購読だけを再接続する', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient((observation) => observations.push(observation))
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()
    const firstTrack = { kind: 'audio', mediaStreamTrack: { id: 'first' } }
    const replacementTrack = { kind: 'audio', mediaStreamTrack: { id: 'replacement' } }
    const publication = { trackSid: 'TR_audio', trackName: 'ds-response-v1:50000000-0000-4000-8000-000000000001' }

    room.emit('trackSubscribed', firstTrack, publication, {})
    await vi.waitFor(() => {
      expect(audioContexts).toHaveLength(1)
      expect(audioContexts[0].worklets).toHaveLength(1)
    })

    room.emit('trackSubscribed', replacementTrack, publication, {})
    expect(audioContexts[0].worklets).toHaveLength(1)
    expect(observations.at(-1)).toMatchObject({
      duplicateTrackFrames: 1,
    })

    room.emit('trackUnsubscribed', firstTrack, publication, {})
    expect(audioContexts[0].worklets[0].disconnect).toHaveBeenCalledTimes(1)
    room.emit('trackSubscribed', replacementTrack, publication, {})

    await vi.waitFor(() => {
      expect(audioContexts[0].worklets).toHaveLength(2)
      expect(observations.at(-1)).toMatchObject({ activeAudioGraphs: 1 })
    })
    client.disconnect()
  })

  test('audio graph初期化失敗後も再接続したtrackを新しいgraphで再生できる', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient((observation) => observations.push(observation))
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()
    workletFailures.push(new Error('audio worklet initialization failed'))

    room.emit(
      'trackSubscribed',
      { kind: 'audio', mediaStreamTrack: { id: 'failed' } },
      { trackSid: 'TR_failed', trackName: 'ds-response-v1:50000000-0000-4000-8000-000000000001' },
      {},
    )
    await vi.waitFor(() => {
      expect(observations.at(-1)).toMatchObject({
        transport: 'unavailable', control: 'unavailable', audio: 'unavailable',
      })
      expect(audioContexts[0].close).toHaveBeenCalledTimes(1)
    })

    room.emit('reconnected')
    room.emit(
      'trackSubscribed',
      { kind: 'audio', mediaStreamTrack: { id: 'recovered' } },
      { trackSid: 'TR_recovered', trackName: 'ds-response-v1:50000000-0000-4000-8000-000000000001' },
      {},
    )

    await vi.waitFor(() => {
      expect(audioContexts).toHaveLength(2)
      expect(audioContexts[1].worklets).toHaveLength(1)
      expect(observations.at(-1)).toMatchObject({
        transport: 'available', audio: 'available', activeAudioGraphs: 1,
      })
    })
    client.disconnect()
  })

  test('barge-inではaudio graphを即時停止し次responseだけで再開する', async () => {
    const observations: RoomObservation[] = []
    const receiveCoreEvent = vi.fn()
    const client = new LiveKitRoomClient(
      (observation) => observations.push(observation),
      receiveCoreEvent,
    )
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()
    room.emit(
      'trackSubscribed',
      { kind: 'audio', mediaStreamTrack: {} },
      { trackSid: 'TR_audio', trackName: 'ds-response-v1:50000000-0000-4000-8000-000000000001' },
      {},
    )
    await vi.waitFor(() => {
      expect(audioContexts).toHaveLength(1)
      expect(audioContexts[0].worklets).toHaveLength(1)
    })
    expect(audioContexts[0].gains[0].gain.value).toBe(1)
    expect(document.querySelectorAll('audio')).toHaveLength(1)

    expect(client.stopPlayback('50000000-0000-4000-8000-000000000001', 100)).toBe(0)
    expect(client.stopPlayback('50000000-0000-4000-8000-000000000001', 101)).toBe(0)
    expect(audioContexts[0].worklets[0].disconnect).toHaveBeenCalledTimes(1)
    expect(document.querySelectorAll('audio')).toHaveLength(1)
    expect(document.querySelector('audio')?.muted).toBe(true)
    expect(observations.at(-1)).toMatchObject({
      audio: 'unavailable', activeAudioGraphs: 0, speechStartedAtMs: 100,
    })

    emitCoreEvent(room, {
      type: 'response_started',
      protocol_version: '1.0',
      event_id: '10000000-0000-4000-8000-000000000001',
      session_id: '20000000-0000-4000-8000-000000000001',
      response_id: '50000000-0000-4000-8000-000000000002',
      speaker: {
        participant_id: '40000000-0000-4000-8000-000000000001',
        role: 'character',
        character_id: 'miori',
      },
      source_utterance_ids: ['30000000-0000-4000-8000-000000000001'],
      monotonic_timestamp_ms: 1_000,
    })

    await vi.waitFor(() => expect(receiveCoreEvent).toHaveBeenCalledTimes(1))
    const trackEvidence = observations.find((observation) => observation.mediaObservation !== undefined)
    expect(trackEvidence).toMatchObject({
      mediaCorrelationMissingReason: 'response_frame_correlation_unavailable',
    })
    expect(trackEvidence?.mediaResponseId).toBeUndefined()
    const sentMeasurements = room.localParticipant.publishData.mock.calls
      .map(([payload]) => JSON.parse(new TextDecoder().decode(payload)).measurement)
    expect(sentMeasurements).not.toContain('client_track_received')
    expect(sentMeasurements).not.toContain('client_encoded_received')
    expect(sentMeasurements).not.toContain('client_audio_decoded')
    expect(audioContexts).toHaveLength(1)
    expect(document.querySelector('audio')?.muted).toBe(true)

    emitCoreEvent(room, {
      type: 'response_audio_segment',
      protocol_version: '1.0',
      event_id: '10000000-0000-4000-8000-000000000002',
      session_id: '20000000-0000-4000-8000-000000000001',
      response_id: '50000000-0000-4000-8000-000000000002',
      audio_sequence: 1,
      text_range: { start: 0, end: 1 },
      monotonic_timestamp_ms: 1_100,
    })

    room.emit('trackSubscribed', { kind: 'audio', mediaStreamTrack: {} }, {
      trackSid: 'TR_next', trackName: 'ds-response-v1:50000000-0000-4000-8000-000000000002',
    }, {})
    await vi.waitFor(() => {
      expect(audioContexts[0].worklets).toHaveLength(2)
      expect(observations.at(-1)).toMatchObject({ activeAudioGraphs: 1 })
    })
    // 旧trackは残っていても再開しない。新しい応答のtrackだけを接続する。
    expect(audioContexts[0].worklets[0].connect).toHaveBeenCalledTimes(1)
    expect(audioContexts[0].worklets[1].connect).toHaveBeenCalledTimes(1)
    client.disconnect()
  })

  test('再接続中のbarge-in観測はtransport unavailableを維持する', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient((observation) => observations.push(observation))
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()

    room.emit('reconnecting')
    client.stopPlayback('50000000-0000-4000-8000-000000000001', 100)

    expect(observations.at(-1)).toMatchObject({
      transport: 'unavailable', control: 'unavailable', audio: 'unavailable',
      speechStartedAtMs: 100,
    })
    client.disconnect()
  })

  test('画面要求topicだけを画面取得callbackへ分離して渡す', async () => {
    const receiveCoreEvent = vi.fn()
    const receiveScreenRequest = vi.fn()
    const client = new LiveKitRoomClient(
      () => undefined,
      receiveCoreEvent,
      undefined,
      receiveScreenRequest,
    )
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    const room = latestRoom()
    const event = {
      protocol_version: '1.0',
      type: 'screen_snapshot_requested',
      event_id: '10000000-0000-4000-8000-000000000006',
      screen_session_id: '40000000-0000-4000-8000-000000000001',
      generation: 1,
      request_id: '50000000-0000-4000-8000-000000000001',
      turn_id: '60000000-0000-4000-8000-000000000001',
      source: 'natural_language_voice',
      requested_at: '2026-09-06T03:00:06Z',
      capture_deadline: '2026-09-06T03:00:11Z',
    }

    emitScreenEvent(room, event)

    await vi.waitFor(() => expect(receiveScreenRequest).toHaveBeenCalledTimes(1))
    expect(receiveScreenRequest).toHaveBeenCalledWith(event)
    expect(receiveCoreEvent).not.toHaveBeenCalled()
    client.disconnect()
  })
  test('応答IDを持たないtrackを再生経路へ接続しない', async () => {
    const client = new LiveKitRoomClient(() => undefined)
    await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
    latestRoom().emit('trackSubscribed', { kind: 'audio', mediaStreamTrack: {} }, { trackSid: 'TR_unknown', trackName: 'character-response' }, {})
    await Promise.resolve()
    expect(audioContexts).toHaveLength(0)
    client.disconnect()
  })

  test('停止した応答のtrackは同一sessionへ再接続しても復活しない', async () => {
    const client = new LiveKitRoomClient(() => undefined)
    const sessionId = '20000000-0000-4000-8000-000000000001'
    const responseId = '50000000-0000-4000-8000-000000000001'
    await client.connect('ws://127.0.0.1:7880', 'token', sessionId)
    client.stopPlayback(responseId, 10)
    client.temporaryDisconnect()
    await client.connect('ws://127.0.0.1:7880', 'new-token', sessionId)
    latestRoom().emit('trackSubscribed', { kind: 'audio', mediaStreamTrack: {} }, { trackSid: 'TR_old', trackName: 'ds-response-v1:' + responseId }, {})
    await Promise.resolve()
    expect(audioContexts).toHaveLength(0)
    client.disconnect()
  })


test('復号PCMは一つのworkletへ渡し、停止後の旧PCMを再投入しない', async () => {
  const client = new LiveKitRoomClient(() => undefined)
  await client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000001')
  const room = latestRoom()
  const responseId = '22222222-2222-2222-2222-222222222222'
  room.emit('trackSubscribed', {kind: 'audio', mediaStreamTrack: {}},
    {trackSid: 'TR_packet', trackName: `ds-response-v1:${responseId}`})
  await vi.waitFor(() => expect(audioContexts.at(-1)?.worklets).toHaveLength(1))
  const worklet = audioContexts.at(-1)!.worklets[0]
  const observer = mediaMocks.observers.at(-1)!
  const pcm = new Float32Array(960).fill(.25)
  observer.playback!.packet({packetIndex: 0, rtpTimestamp: 99, receivedAtMs: 100, decodedAtMs: 101, pcm})
  expect(worklet.port.postMessage).toHaveBeenCalledWith(
    {kind: 'pcm', packetIndex: 0, rtpTimestamp: 99, samples: pcm}, [pcm.buffer])
  client.stopPlayback(responseId)
  expect(worklet.port.postMessage).toHaveBeenCalledWith({kind: 'stop'})
  observer.playback!.packet({packetIndex: 1, rtpTimestamp: 1059, receivedAtMs: 120, decodedAtMs: 121, pcm})
  expect(worklet.port.postMessage.mock.calls.filter(([row]) => row.kind === 'pcm')).toHaveLength(1)
  client.disconnect()
})


test.each(['valid', 'mismatched_decode', 'stopped', 'unsubscribed'])('実出力した応答packetだけをmedia traceへ相関する（%s）', async mode => {
  const observations: RoomObservation[] = []
  const packetOutputs: Array<{status: string}> = []
  const client = new LiveKitRoomClient(row => observations.push(row))
  client.setPacketOutputObserver(row => packetOutputs.push(row))
  const responseId = '22222222-2222-2222-2222-222222222222'
  await client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000001')
  const room = latestRoom()
  const track = {kind: 'audio', mediaStreamTrack: {}}
  const publication = {trackSid: 'TR_correlated', trackName: `ds-response-v1:${responseId}`}
  room.emit('trackSubscribed', track, publication)
  await vi.waitFor(() => expect(audioContexts.at(-1)?.worklets).toHaveLength(1))
  const observer = mediaMocks.observers.at(-1)!
  const worklet = audioContexts.at(-1)!.worklets[0]
  const measurements = () => room.localParticipant.publishData.mock.calls
    .map(([payload]) => JSON.parse(new TextDecoder().decode(payload)))
    .filter(row => ['client_track_received', 'client_encoded_received', 'client_audio_decoded'].includes(row.measurement))
  try {
    observer.report({trackReceivedAtMs: 50, firstPacketReceivedAtMs: 100,
      firstPacketDecodedAtMs: mode === 'mismatched_decode' ? 102 : 101,
      firstPacketDecodedSamples: 960, firstPacketDeliveredAtMs: 150})
    observer.playback!.packet({packetIndex: 0, rtpTimestamp: 99, receivedAtMs: 100,
      decodedAtMs: 101, receivedAtBoundsMs: {lowerMs: 100, upperMs: 100.2},
      decodedAtBoundsMs: {lowerMs: 101, upperMs: 101.2}, source: 7, pcm: new Float32Array(960).fill(.25)})
    expect(measurements()).toHaveLength(0)
    expect(observations.some(row => row.mediaResponseId)).toBe(false)
    if (mode === 'stopped') client.stopPlayback(responseId)
    if (mode === 'unsubscribed') room.emit('trackUnsubscribed', track, publication)
    worklet.port.onmessage?.({data: {kind: 'rendered', packetIndex: 0, rtpTimestamp: 99,
      packetSampleOffset: 0, startFrame: 24000, endFrame: 24128, energy: .25,
      renderQuantumStartFrame: 24000, renderClockConfirmationFrame: 24000}} as MessageEvent)
    if (mode === 'valid') {
      await vi.waitFor(() => expect(measurements()).toHaveLength(3))
      expect(measurements().map(row => [row.measurement, row.timestamp, row.response_id])).toEqual([
        ['client_track_received', 50, responseId], ['client_encoded_received', 100, responseId],
        ['client_audio_decoded', 101, responseId],
      ])
      const correlated = observations.find(row => row.mediaResponseId === responseId)
      expect(correlated?.packetPlaybackObservation?.firstOutputAtMs).toBe(500)
      expect(correlated?.packetPlaybackObservation?.decodedAtMs).toBe(101)
      expect(packetOutputs).toHaveLength(1)
      expect(packetOutputs[0].status).toBe('captured')
    } else {
      if (mode === 'mismatched_decode') await vi.waitFor(() => expect(observations.some(row => row.failureStage)).toBe(true))
      else await new Promise(resolve => setTimeout(resolve, 20))
      expect(measurements()).toHaveLength(0)
      expect(observations.some(row => row.mediaResponseId)).toBe(false)
    }
  } finally {client.disconnect()}
})


test.each(['gap', 'overlap'])('RTP不連続では未出力のPCMを止め、Coreの応答だけを中断して制御接続を維持する: %s', async mode => {
  const observations: RoomObservation[] = []
  const client = new LiveKitRoomClient(row => observations.push(row))
  const sessionId = '20000000-0000-4000-8000-000000000001'
  const responseId = '22222222-2222-2222-2222-222222222222'
  await client.connect('ws://test', 'token', sessionId)
  const room = latestRoom(), disconnected = vi.spyOn(room, 'disconnect')
  room.emit('trackSubscribed', {kind: 'audio', mediaStreamTrack: {}},
    {trackSid: 'TR_loss', trackName: `ds-response-v1:${responseId}`})
  await vi.waitFor(() => expect(audioContexts.at(-1)?.worklets).toHaveLength(1))
  const observer = mediaMocks.observers.at(-1)!, worklet = audioContexts.at(-1)!.worklets[0]
  const frame = {receivedAtMs: 100, decodedAtMs: 101, pcm: new Float32Array(960).fill(.25)}
  try {
    observer.playback!.packet({...frame, packetIndex: 0, rtpTimestamp: 99})
    if (mode === 'overlap') {observer.playback!.interrupted!(); observer.playback!.interrupted!()}
    else observer.playback!.packet({...frame, packetIndex: 1, rtpTimestamp: 2019})
    observer.playback!.packet({...frame, packetIndex: 2, rtpTimestamp: 2979})
    const messages = () => room.localParticipant.publishData.mock.calls.map(([p]) => JSON.parse(new TextDecoder().decode(p)))
    await vi.waitFor(() => expect(messages().some(p => p.type === 'state_sync_request')).toBe(true))
    expect(messages().filter(p => ['playback_stopped', 'response_cancel_requested'].includes(p.type)))
      .toMatchObject([{type: 'playback_stopped', session_id: sessionId, response_id: responseId,
        reason: 'disconnect', last_played_audio_sequence: 0},
      {type: 'response_cancel_requested', session_id: sessionId, response_id: responseId, reason: 'disconnect'}])
    expect(worklet.port.postMessage.mock.calls.filter(([row]) => row.kind === 'pcm')).toHaveLength(1)
    expect(worklet.port.postMessage).toHaveBeenCalledWith({kind: 'stop'})
    if (mode === 'gap') {
      expect(observations.find(row => row.mediaPacketLoss)?.mediaPacketLoss)
        .toMatchObject({responseId, expectedTimestamp: 1059, receivedTimestamp: 2019, missingPacketCount: 1})
      expect(observations.filter(row => row.mediaPacketLoss)).toHaveLength(1)
    } else {
      expect(observations.filter(row => row.mediaTimelineInterruption)).toHaveLength(1)
      expect(observations.find(row => row.mediaTimelineInterruption)?.mediaTimelineInterruption)
        .toMatchObject({responseId, reason: 'timestamp_overlap'})
    }
    expect(observations.some(row => row.failureStage)).toBe(false)
    expect(disconnected).not.toHaveBeenCalled()
    expect(messages().filter(p => ['playback_stopped', 'response_cancel_requested', 'state_sync_request'].includes(p.type))
      .map(p => p.type)).toEqual(['playback_stopped', 'response_cancel_requested', 'state_sync_request'])
    emitPrivateFrame(room, authoritativeState(1, [{type: 'response_interrupted', session_id: sessionId,
      response_id: responseId, confirmed_audio_sequence: 0}]))
    await vi.waitFor(() => expect(audioContexts[0].close).toHaveBeenCalled())
    expect(audioContexts.flatMap(context => context.worklets)).toHaveLength(1)
    const probe = client.probeControl()
    const sent = messages().find(row => row.type === 'control_probe')
    emitPrivateFrame(room, new TextEncoder().encode(JSON.stringify({...sent, type: 'control_probe_ack'})))
    expect(await probe).toMatchObject({status: 'received', generation: 1})
  } finally {client.disconnect()}
})

})

test('実roomのprobe応答をnonce・世代へ相関し、通常の状態同期を追加送信しない', async () => {
  const observations: RoomObservation[] = []
  const client = new LiveKitRoomClient(value => observations.push(value))
  expect((await client.probeControl()).status).toBe('unavailable')
  await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
  const room = latestRoom()
  room.localParticipant.publishData.mockClear()
  const count = observations.length
  const pending = client.probeControl()
  const sent = room.localParticipant.publishData.mock.calls[0]
  const frame = JSON.parse(new TextDecoder().decode(sent[0]))
  expect(frame).toMatchObject({type: 'control_probe', generation: 0})
  expect(sent[1]).toEqual({reliable: true, topic: 'digital-souls.livekit-transport.v1'})
  emitPrivateFrame(room, new TextEncoder().encode(JSON.stringify({...frame, type: 'control_probe_ack'})))
  expect(await pending).toMatchObject({status: 'received', probeId: frame.probe_id, generation: 0})
  expect(observations.length).toBe(count)
  expect(room.localParticipant.publishData).toHaveBeenCalledTimes(1)
  client.disconnect()
  expect((await client.probeControl()).status).toBe('unavailable')
})

test.each(['reconnecting', 'disconnected', 'generation'])('接続変化でprobe待機を終了する: %s', async change => {
  const client = new LiveKitRoomClient(() => undefined)
  await client.connect('ws://127.0.0.1:7880', 'token', '20000000-0000-4000-8000-000000000001')
  const room = latestRoom()
  const pending = client.probeControl()
  if (change === 'generation') emitPrivateFrame(room, authoritativeState(1))
  else room.emit(change)
  expect((await pending).status).toBe('interrupted')
  client.disconnect()
})
