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
    SignalReconnecting: 'signalReconnecting',
    SignalConnected: 'signalConnected',
    Reconnecting: 'reconnecting',
    Reconnected: 'reconnected',
    DataReceived: 'dataReceived',
    TrackPublished: 'trackPublished',
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

  connect = vi.fn((destination: unknown): unknown => destination)
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

  constructor(readonly options?: AudioContextOptions) {
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

  test('明示診断はgain後段を通り、cancel時に監視を切断せずcontext closeもdrainを待つ', async () => {
    const rows: import('./livekit/post-gain-monitor').StaleAudioObservation[] = []
    const client = new LiveKitRoomClient(() => undefined)
    client.setStaleAudioObserver(row => rows.push(row))
    const responseId = '50000000-0000-4000-8000-000000000001'
    const sessionId = '20000000-0000-4000-8000-000000000001'
    let now = 1000
    const time = vi.spyOn(performance, 'now').mockImplementation(() => now)
    try {
      await client.connect('ws://test', 'token', sessionId)
      const room = latestRoom()
      room.emit('trackSubscribed', {kind: 'audio', mediaStreamTrack: {}},
        {trackSid: 'TR_audited', trackName: `ds-response-v1:${responseId}`})
      await vi.waitFor(() => expect(audioContexts[0]?.worklets).toHaveLength(2))
      const context = audioContexts[0], [renderer, audit] = context.worklets
      expect(context.options).toEqual({sampleRate: 48000, latencyHint: 0})
      expect(context.gains[0].connect).toHaveBeenCalledWith(audit)
      expect(audit.connect).toHaveBeenCalledWith(context.destination)
      const timestamp = vi.spyOn(context, 'getOutputTimestamp').mockReturnValue({contextTime: 1.009, performanceTime: 1009})
      now = 1009.5
      audit.port.onmessage?.({data: {kind: 'output', confirmedFrame: 49920, intervals: Array.from({length: 16}, (_, i) => ({
        startFrame: 48000 + i * 128, endFrame: 48128 + i * 128, nonzeroSamples: i === 0 ? 128 : 0,
        firstNonzeroFrame: i === 0 ? 48000 : null, lastNonzeroFrame: i === 0 ? 48127 : null,
      }))}} as MessageEvent)
      now = 1010
      emitCoreEvent(room, {protocol_version: '1.0', type: 'response_cancelled',
        event_id: '60000000-0000-4000-8000-000000000003', session_id: sessionId, response_id: responseId,
        reason: 'barge_in', monotonic_timestamp_ms: 2002})
      expect(renderer.disconnect).toHaveBeenCalledOnce()
      expect(audit.disconnect).not.toHaveBeenCalled()
      expect(rows.at(-1)?.audit.complete).toBe(false)
      mediaMocks.observers[0].playback!.packet({pcm: new Float32Array(960)})
      expect(rows.at(-1)?.receivedAfterCancelPackets).toBe(1)
      client.disconnect()
      expect(context.close).not.toHaveBeenCalled()
      expect(audit.port.close).not.toHaveBeenCalled()
      now = 1012.5; timestamp.mockReturnValue({contextTime: 1.012, performanceTime: 1012})
      await new Promise(resolve => setTimeout(resolve, 10))
      now = 1051; timestamp.mockReturnValue({contextTime: 1.05, performanceTime: 1050})
      audit.port.onmessage?.({data: {kind: 'finished', endFrame: 50048}} as MessageEvent)
      await vi.waitFor(() => expect(context.close).toHaveBeenCalledOnce())
      expect(audit.disconnect).toHaveBeenCalledOnce()
      expect(rows.at(-1)).toMatchObject({responseId, sessionId, graphClosed: true,
        audit: {complete: true, nonzeroSamplesAfterCancelUpper: 0}})
    } finally {client.disconnect(); time.mockRestore()}
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


test.each(['gap', 'overlap', 'ragged_gap', 'gap_during_resume'])('RTP不連続では未出力のPCMを止め、Coreの応答だけを中断して制御接続を維持する: %s', async mode => {
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
    if (mode === 'gap_during_resume') room.emit('signalReconnecting')
    if (mode === 'overlap') {observer.playback!.interrupted!(); observer.playback!.interrupted!()}
    else observer.playback!.packet({...frame, packetIndex: 1, rtpTimestamp: mode === 'ragged_gap' ? 2021 : 2019})
    observer.playback!.packet({...frame, packetIndex: 2, rtpTimestamp: 2979})
    const messages = () => room.localParticipant.publishData.mock.calls.map(([p]) => JSON.parse(new TextDecoder().decode(p)))
    if (mode === 'gap_during_resume') {
      await vi.waitFor(() => expect(messages().filter(p => p.type === 'response_cancel_requested')).toHaveLength(1))
      await new Promise(resolve => setTimeout(resolve, 0))
      expect(messages().filter(p => p.type === 'state_sync_request')).toHaveLength(1)
      expect(client.isAudioProbeReady()).toBe(false)
      room.emit('reconnected')
    }
    await vi.waitFor(() => expect(messages().some(p => p.type === 'state_sync_request')).toBe(true))
    expect(messages().filter(p => ['playback_stopped', 'response_cancel_requested'].includes(p.type)))
      .toMatchObject([{type: 'playback_stopped', session_id: sessionId, response_id: responseId,
        reason: 'disconnect', last_played_audio_sequence: 0},
      {type: 'response_cancel_requested', session_id: sessionId, response_id: responseId, reason: 'disconnect'}])
    expect(worklet.port.postMessage.mock.calls.filter(([row]) => row.kind === 'pcm')).toHaveLength(1)
    expect(worklet.port.postMessage).toHaveBeenCalledWith({kind: 'stop'})
    if (mode === 'gap' || mode === 'gap_during_resume') {
      expect(observations.find(row => row.mediaPacketLoss)?.mediaPacketLoss)
        .toMatchObject({responseId, expectedTimestamp: 1059, receivedTimestamp: 2019, missingPacketCount: 1})
      expect(observations.filter(row => row.mediaPacketLoss)).toHaveLength(1)
    } else {
      expect(observations.filter(row => row.mediaTimelineInterruption)).toHaveLength(1)
      expect(observations.find(row => row.mediaTimelineInterruption)?.mediaTimelineInterruption)
        .toMatchObject({responseId, reason: mode === 'ragged_gap' ? 'timestamp_discontinuity' : 'timestamp_overlap'})
    }
    expect(observations.some(row => row.failureStage)).toBe(false)
    expect(disconnected).not.toHaveBeenCalled()
    expect(messages().filter(p => ['playback_stopped', 'response_cancel_requested', 'state_sync_request'].includes(p.type))
      .map(p => p.type)).toEqual(mode === 'gap_during_resume'
        ? ['playback_stopped', 'state_sync_request', 'response_cancel_requested']
        : ['playback_stopped', 'response_cancel_requested', 'state_sync_request'])
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


const probeSession = '20000000-0000-4000-8000-000000000001'
const probePublisher = {identity: 'character-miori-' + probeSession, sid: 'PA_backend'}
const probeMessages = (room: InstanceType<typeof livekitMocks.FakeRoom>) => room.localParticipant.publishData.mock.calls
  .map(([payload]) => JSON.parse(new TextDecoder().decode(payload)))
const finishAudioProbe = (room: InstanceType<typeof livekitMocks.FakeRoom>, nonce: string, extra = {}, publisher = probePublisher) => {
  room.emit('dataReceived', new TextEncoder().encode(JSON.stringify({protocol_version: '1.0', type: 'audio_probe_finished',
    generation: 0, probe_id: nonce, track_sid: 'TR_probe', input_sample_count: 9600,
    captured_sample_count: 10560, padding_sample_count: 960, ...extra})), publisher, undefined, 'digital-souls.livekit-transport.v1')
}

test('診断音は全packetの実出力時計を待ち、会話の再生・Core測定を変更しない', async () => {
  vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance']})
  const observations: RoomObservation[] = []
  const client = new LiveKitRoomClient(value => observations.push(value))
  try {
    await client.connect('ws://test', 'token', probeSession)
    const room = latestRoom(), pending = client.probeAudio()
    const request = probeMessages(room).find(row => row.type === 'audio_probe_request')
    const publication = {trackSid: 'TR_probe', trackName: 'ds-audio-probe-v1:' + request.probe_id}
    const track = {kind: 'audio', mediaStreamTrack: {}}
    room.emit('trackSubscribed', track, {...publication, trackName: 'ds-audio-probe-v1:other'}, probePublisher)
    room.emit('trackSubscribed', track, publication, {...probePublisher, identity: 'user-' + probeSession})
    expect(audioContexts).toHaveLength(0)
    room.emit('trackSubscribed', track, publication, probePublisher)
    await vi.advanceTimersByTimeAsync(1)
    const context = audioContexts[0], worklet = context.worklets[0], observer = mediaMocks.observers[0]
    expect(probeMessages(room).filter(row => row.type === 'audio_probe_ready')).toHaveLength(1)
    let passed = false, resolved = false
    void pending.then(() => {resolved = true})
    vi.spyOn(context, 'getOutputTimestamp').mockImplementation(() => ({contextTime: passed ? 1 : .05, performanceTime: passed ? 1000 : 50}))
    for (let index = 0; index < 11; index++) {
      observer.playback!.packet({packetIndex: index, rtpTimestamp: 99 + index * 960, source: 7,
        receivedAtMs: 10 + index * 20, decodedAtMs: 11 + index * 20,
        receivedAtBoundsMs: {lowerMs: 10 + index * 20, upperMs: 10.2 + index * 20},
        decodedAtBoundsMs: {lowerMs: 11 + index * 20, upperMs: 11.2 + index * 20}, pcm: new Float32Array(960).fill(.25)})
      worklet.port.onmessage?.({data: {kind: 'rendered', packetIndex: index, rtpTimestamp: 99 + index * 960,
        packetSampleOffset: 0, startFrame: 4800 + index * 960, endFrame: 5760 + index * 960,
        energy: .25, firstAudibleFrame: 4800 + index * 960}} as MessageEvent)
    }
    finishAudioProbe(room, request.probe_id, {generation: 1})
    finishAudioProbe(room, request.probe_id, {track_sid: 'TR_other'})
    finishAudioProbe(room, request.probe_id, {}, {...probePublisher, sid: 'PA_old'})
    await vi.advanceTimersByTimeAsync(1000)
    expect(resolved).toBe(false)
    passed = true
    await vi.advanceTimersByTimeAsync(5)
    expect(resolved).toBe(false) // 不一致の完了通知では出力済みでも完了しない。
    finishAudioProbe(room, request.probe_id)
    const result = await pending
    expect(result).toMatchObject({scope: 'rtc_audio_probe', status: 'captured', cleanupCompleted: true,
      completion: {renderedSamples: 10560, packetCount: 11, gapSamples: 0}})
    expect(result.packetOutputs).toHaveLength(11)
    expect(result.trackEvents.map(row => [row.nameMatches, row.publisherMatches])).toEqual([[false, true], [true, false], [true, true]])
    expect(JSON.stringify(result.packetOutputs)).not.toContain('"pcm"')
    expect(context.close).toHaveBeenCalledOnce()
    expect(worklet.disconnect).toHaveBeenCalledOnce()
    expect(document.querySelector('audio')).toBeNull()
    expect(probeMessages(room).map(row => row.type)).toEqual(['audio_probe_request', 'audio_probe_ready', 'audio_probe_complete'])
    expect(observations.some(row => row.playbackCompletedResponseId || row.mediaResponseId)).toBe(false)
  } finally {client.disconnect(); vi.useRealTimers()}
})

test.each(['reconnecting', 'disconnected', 'generation', 'unsubscribed', 'timeout', 'decoder'])('音声診断の失敗を保持してgraphを閉じる: %s', async mode => {
  vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval']})
  const client = new LiveKitRoomClient(() => undefined)
  try {
    await client.connect('ws://test', 'token', probeSession)
    const room = latestRoom(), pending = client.probeAudio()
    const request = probeMessages(room).find(row => row.type === 'audio_probe_request')
    const publication = {trackSid: 'TR_probe', trackName: 'ds-audio-probe-v1:' + request.probe_id}
    room.emit('trackSubscribed', {kind: 'audio', mediaStreamTrack: {}}, publication, probePublisher)
    await vi.advanceTimersByTimeAsync(1)
    if (mode === 'generation') emitPrivateFrame(room, authoritativeState(1))
    else if (mode === 'unsubscribed') room.emit('trackUnsubscribed', {}, publication)
    else if (mode === 'decoder') mediaMocks.observers.at(-1)!.playback!.failed()
    else if (mode === 'timeout') await vi.advanceTimersByTimeAsync(10000)
    else room.emit(mode)
    expect(await pending).toMatchObject({status: 'failed', cleanupCompleted: true,
      reason: mode === 'unsubscribed' ? 'track_unsubscribed' : mode === 'decoder' ? 'media_decoder'
        : mode === 'timeout' ? 'timeout' : 'connection_changed'})
    expect(audioContexts[0].close).toHaveBeenCalledOnce()
    expect(document.querySelector('audio')).toBeNull()
    expect(probeMessages(room).some(row => row.type === 'audio_probe_complete')).toBe(false)
  } finally {client.disconnect(); vi.useRealTimers()}
})

test('音声診断は未接続・同時要求を失敗として返し、余分なtrackを要求しない', async () => {
  const client = new LiveKitRoomClient(() => undefined)
  expect(await client.probeAudio()).toMatchObject({status: 'failed', reason: 'unavailable'})
  await client.connect('ws://test', 'token', probeSession)
  const pending = client.probeAudio()
  expect(await client.probeAudio()).toMatchObject({status: 'failed', reason: 'busy'})
  expect(probeMessages(latestRoom()).filter(row => row.type === 'audio_probe_request')).toHaveLength(1)
  client.disconnect()
  expect(await pending).toMatchObject({status: 'failed', reason: 'connection_changed'})
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


test('接続診断はsignal再接続とCore世代の時系列だけを通知する', async () => {
  const client = new LiveKitRoomClient(() => undefined), observed: unknown[] = []
  client.setConnectionObserver(row => observed.push(row))
  await client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000001')
  const room = latestRoom()
  room.emit('signalReconnecting')
  room.emit('signalConnected')
  room.emit('reconnecting')
  room.emit('reconnected')
  await Promise.resolve()
  emitPrivateFrame(room, authoritativeState(1))
  client.disconnect()
  expect(observed).toEqual([
    expect.objectContaining({event: 'signal_reconnecting', generation: 0}),
    expect.objectContaining({event: 'signal_connected', generation: 0}),
    expect.objectContaining({event: 'reconnecting', generation: 0}),
    expect.objectContaining({event: 'reconnected', generation: 0}),
    expect.objectContaining({event: 'state_sync_requested', generation: 0}),
    expect.objectContaining({event: 'authoritative_state', generation: 1}),
    expect.objectContaining({event: 'disconnected', generation: 1}),
  ])
})


test('新世代の状態同期が終わるまで診断音を要求しない', async () => {
  const client = new LiveKitRoomClient(() => undefined)
  await client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000001')
  const room = latestRoom()
  try {
    expect(client.isAudioProbeReady()).toBe(true)
    room.emit('signalReconnecting')
    expect(client.isAudioProbeReady()).toBe(false)
    expect(await client.probeAudio()).toMatchObject({reason: 'unavailable'})
    room.emit('reconnected')
    await Promise.resolve()
    expect(client.isAudioProbeReady()).toBe(false)
    emitPrivateFrame(room, authoritativeState(0))
    expect(client.isAudioProbeReady()).toBe(false)
    emitPrivateFrame(room, authoritativeState(1))
    expect(client.isAudioProbeReady()).toBe(true)
    const messages = room.localParticipant.publishData.mock.calls.map(([p]) => JSON.parse(new TextDecoder().decode(p)))
    expect(messages.filter(p => p.type === 'state_sync_request')).toHaveLength(1)
    expect(messages.filter(p => p.type === 'audio_probe_request')).toHaveLength(0)
  } finally {client.disconnect()}
})

test('CoreイベントはACK送信失敗中にも一度だけ適用し、ACK再送で接続を維持する', async () => {
  vi.useFakeTimers()
  const receive = vi.fn(), delivery = vi.fn(), client = new LiveKitRoomClient(() => undefined, receive)
  client.setCoreDeliveryObserver(delivery)
  await client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000010')
  const room = latestRoom(), disconnected = vi.spyOn(room, 'disconnect')
  const event = {protocol_version: '1.0', event_id: '10000000-0000-4000-8000-000000000010',
    type: 'response_delta', session_id: '20000000-0000-4000-8000-000000000010',
    response_id: '30000000-0000-4000-8000-000000000010', text_sequence: 1, text: 'a',
    text_range: {start: 0, end: 1}, monotonic_timestamp_ms: 1}
  try {
    room.emit('signalReconnecting')
    room.localParticipant.publishData.mockRejectedValueOnce(new Error('disconnected'))
    emitCoreEvent(room, event)
    await vi.advanceTimersByTimeAsync(0)
    expect(receive).toHaveBeenCalledTimes(1)
    expect(disconnected).not.toHaveBeenCalled()
    emitCoreEvent(room, event)
    await vi.advanceTimersByTimeAsync(250)
    expect(receive).toHaveBeenCalledTimes(1)
    expect(delivery).toHaveBeenCalledTimes(2)
    expect(delivery.mock.calls.map(([row]) => row.duplicate)).toEqual([false, true])
    expect(delivery.mock.calls[1][0]).toMatchObject({type: 'response_delta',
      responseId: event.response_id, textCharacters: 1, textSequence: 1})
    expect(delivery.mock.calls[1][0]).not.toHaveProperty('text')
    expect(room.localParticipant.publishData.mock.calls.filter(([p]) =>
      JSON.parse(new TextDecoder().decode(p)).type === 'ack')).toHaveLength(2)
    expect(disconnected).not.toHaveBeenCalled()
  } finally {client.disconnect(); vi.useRealTimers()}
})


test.each(['send_failed', 'reply_missing'])('状態同期は同じ要求世代で再送し、確認後は世代を再更新しない: %s', async mode => {
  vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance']})
  const observations: RoomObservation[] = [], client = new LiveKitRoomClient(row => observations.push(row))
  await client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000001')
  const room = latestRoom(), disconnected = vi.spyOn(room, 'disconnect')
  const requests = () => room.localParticipant.publishData.mock.calls.map(([p]) => JSON.parse(new TextDecoder().decode(p)))
    .filter(p => p.type === 'state_sync_request')
  try {
    if (mode === 'send_failed') room.localParticipant.publishData.mockRejectedValueOnce(new Error('disconnected'))
    room.emit('signalReconnecting'); room.emit('reconnected')
    await vi.advanceTimersByTimeAsync(500)
    expect(requests().map(p => p.generation)).toEqual([0, 0, 0])
    expect(client.isAudioProbeReady()).toBe(false)
    expect(disconnected).not.toHaveBeenCalled()
    emitPrivateFrame(room, authoritativeState(1))
    expect(client.isAudioProbeReady()).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(requests()).toHaveLength(3)
    expect(observations.some(row => row.failureStage)).toBe(false)
  } finally {client.disconnect(); vi.useRealTimers()}
})


test('制御の同期確認後もSDKのmedia再接続完了まで診断音を送らず、同期を繰り返さない', async () => {
  vi.useFakeTimers({toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'performance']})
  const client = new LiveKitRoomClient(() => undefined)
  await client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000001')
  const room = latestRoom()
  const messages = () => room.localParticipant.publishData.mock.calls.map(([p]) => JSON.parse(new TextDecoder().decode(p)))
  try {
    room.emit('signalReconnecting')
    await vi.advanceTimersByTimeAsync(0)
    expect(messages().filter(p => p.type === 'state_sync_request').map(p => p.generation)).toEqual([0])
    expect(client.isAudioProbeReady()).toBe(false)
    emitPrivateFrame(room, authoritativeState(1))
    expect(client.isAudioProbeReady()).toBe(false)
    expect(await client.probeAudio()).toMatchObject({reason: 'unavailable'})
    expect(messages().filter(p => p.type === 'audio_probe_request')).toHaveLength(0)
    room.emit('reconnected')
    expect(client.isAudioProbeReady()).toBe(true)
    const probe = client.probeAudio()
    await vi.advanceTimersByTimeAsync(250)
    expect(messages().filter(p => p.type === 'state_sync_request')).toHaveLength(1)
    expect(messages().filter(p => p.type === 'audio_probe_request')).toHaveLength(1)
    // 遅着した旧世代の状態で同期確認を巻き戻さない。
    emitPrivateFrame(room, authoritativeState(0))
    expect(client.isAudioProbeReady()).toBe(true)
    room.emit('signalReconnecting')
    expect(await probe).toMatchObject({reason: 'connection_changed'})
    expect(client.isAudioProbeReady()).toBe(false)
    await vi.advanceTimersByTimeAsync(0)
    expect(messages().filter(p => p.type === 'state_sync_request').map(p => p.generation)).toEqual([0, 1])
    emitPrivateFrame(room, authoritativeState(1))
    expect(client.isAudioProbeReady()).toBe(false)
    emitPrivateFrame(room, authoritativeState(2))
    expect(client.isAudioProbeReady()).toBe(false)
    room.emit('reconnecting')
    room.emit('reconnected')
    expect(client.isAudioProbeReady()).toBe(false)
    await vi.advanceTimersByTimeAsync(0)
    emitPrivateFrame(room, authoritativeState(3))
    expect(client.isAudioProbeReady()).toBe(true)
  } finally {client.disconnect(); vi.useRealTimers()}
})


test.each([[3, 3], [undefined, null], ['token-must-not-appear', null], [{token: 'token-must-not-appear'}, null], [NaN, null], [Infinity, null]])(
  'SDK切断診断は理由の数値だけを記録する: %s', async (reason, expectedReason) => {
    const client = new LiveKitRoomClient(() => undefined), observed: unknown[] = []
    client.setConnectionObserver(row => observed.push(row))
    await client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000001')
    latestRoom().emit('disconnected', reason)
    expect(observed).toEqual([expect.objectContaining({event: 'disconnected', disconnect: {
      reason: expectedReason,
      origin: 'sdk',
    }})])
    expect(JSON.stringify(observed)).not.toContain('token-must-not-appear')
    client.disconnect()
  },
)

test.each(['explicit', 'temporary', 'transport_failure'] as const)('アプリ起点の切断をSDKの切断と区別する: %s', async origin => {
  const client = new LiveKitRoomClient(() => undefined), observed: unknown[] = []
  client.setConnectionObserver(row => observed.push(row))
  await client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000001')
  if (origin === 'explicit') client.disconnect()
  else if (origin === 'temporary') client.temporaryDisconnect()
  else emitPrivateFrame(latestRoom(), new TextEncoder().encode('invalid private frame'))
  expect(observed).toEqual([expect.objectContaining({event: 'disconnected', disconnect: {reason: null, origin}})])
  client.disconnect()
})


test('時計probeと復旧用control probeは別pendingを持ち、切断時に両方を終了する', async () => {
  const client = new LiveKitRoomClient(() => undefined)
  await client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000010')
  try {
    const control = client.probeControl(), clock = client.probeClock(), room = latestRoom()
    const frames = room.localParticipant.publishData.mock.calls.map(([p]) => JSON.parse(new TextDecoder().decode(p)))
    const request = frames.find(frame => frame.type === 'control_probe' && frame.observe_clock === true)
    expect(frames.filter(frame => frame.type === 'control_probe')).toHaveLength(2)
    emitPrivateFrame(room, new TextEncoder().encode(JSON.stringify({protocol_version: '1.0',
      type: 'control_probe_ack', probe_id: request.probe_id, generation: request.generation,
      server_received_us: 1000, server_sent_us: 1001})))
    expect(await clock).toMatchObject({status: 'received', serverReceivedAtUs: 1000, serverSentAtUs: 1001})
    const interrupted = client.probeClock()
    client.disconnect()
    expect((await control).status).toBe('interrupted')
    expect((await interrupted).status).toBe('interrupted')
  } finally {client.disconnect()}
})


test('時計probeはSDK connect完了までpublishせず、接続待ちへ割り込まない', async () => {
  const blocked = deferred()
  const connect = vi.spyOn(livekitMocks.FakeRoom.prototype, 'connect').mockImplementation(() => blocked.promise)
  const client = new LiveKitRoomClient(() => undefined)
  try {
    const connecting = client.connect('ws://test', 'token', '20000000-0000-4000-8000-000000000010')
    const room = latestRoom()
    expect((await client.probeClock()).status).toBe('unavailable')
    expect(room.localParticipant.publishData).not.toHaveBeenCalled()
    blocked.resolve(); await connecting
    const clock = client.probeClock()
    expect(room.localParticipant.publishData).toHaveBeenCalledOnce()
    client.disconnect(); expect((await clock).status).toBe('interrupted')
  } finally {blocked.resolve(); client.disconnect(); connect.mockRestore()}
})
