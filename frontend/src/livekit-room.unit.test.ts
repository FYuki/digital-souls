import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

const livekitMocks = vi.hoisted(() => {
  class FakeRoom {
    readonly handlers = new Map<string, Array<(...args: unknown[]) => void>>()
    readonly localParticipant = {
      publishData: vi.fn(async (_payload: Uint8Array, _options: unknown) => undefined),
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
  Track: { Kind: { Audio: 'audio' } },
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

class FakeAudioSourceNode {
  disconnect = vi.fn()

  connect(node: FakeAudioWorkletNode): FakeAudioWorkletNode {
    return node
  }
}

class FakeAudioWorkletNode {
  readonly port = { onmessage: null as ((event: MessageEvent) => void) | null }
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
  }) }
  readonly sources: FakeAudioSourceNode[] = []

  constructor() {
    audioContexts.push(this)
  }

  async resume(): Promise<void> {}

  readonly close = vi.fn((): Promise<void> => {
    return closeBlockers.shift()?.promise ?? Promise.resolve()
  })

  createMediaStreamSource(): FakeAudioSourceNode {
    const source = new FakeAudioSourceNode()
    this.sources.push(source)
    return source
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

describe('LiveKit Room generation synchronization', () => {
  beforeEach(() => {
    livekitMocks.rooms.length = 0
    audioContexts.length = 0
    closeBlockers.length = 0
    workletFailures.length = 0
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

  test('世代変更時に前世代の終端応答とactive responseを消去する', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient((observation) => observations.push(observation))
    await client.connect('ws://127.0.0.1:7880', 'token', 'session-id')
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
    await client.connect('ws://127.0.0.1:7880', 'token', 'session-id')
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

  test('同一sessionへの明示的な再接続時に状態同期を要求する', async () => {
    const client = new LiveKitRoomClient(() => undefined)
    await client.connect('ws://127.0.0.1:7880', 'token-1', 'session-id')
    const room = latestRoom()

    client.temporaryDisconnect()
    await client.connect('ws://127.0.0.1:7880', 'token-2', 'session-id')

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
    await client.connect('ws://127.0.0.1:7880', 'token', 'session-id')
    const room = latestRoom()
    room.emit(
      'trackSubscribed',
      { kind: 'audio', mediaStreamTrack: {} },
      { trackSid: 'TR_audio' },
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
      expect(audioContexts.reduce((total, context) => total + context.sources.length, 0)).toBe(2)
      expect(observations.at(-1)).toMatchObject({ activeAudioGraphs: 1 })
    })
    client.disconnect()
  })

  test('重複trackを二重再生せずunsubscribe後の同一track再購読だけを再接続する', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient((observation) => observations.push(observation))
    await client.connect('ws://127.0.0.1:7880', 'token', 'session-id')
    const room = latestRoom()
    const firstTrack = { kind: 'audio', mediaStreamTrack: { id: 'first' } }
    const replacementTrack = { kind: 'audio', mediaStreamTrack: { id: 'replacement' } }
    const publication = { trackSid: 'TR_audio' }

    room.emit('trackSubscribed', firstTrack, publication, {})
    await vi.waitFor(() => {
      expect(audioContexts).toHaveLength(1)
      expect(audioContexts[0].sources).toHaveLength(1)
    })

    room.emit('trackSubscribed', replacementTrack, publication, {})
    expect(audioContexts[0].sources).toHaveLength(1)
    expect(observations.at(-1)).toMatchObject({
      duplicateTrackFrames: 1,
    })

    room.emit('trackUnsubscribed', firstTrack, publication, {})
    expect(audioContexts[0].sources[0].disconnect).toHaveBeenCalledTimes(1)
    room.emit('trackSubscribed', replacementTrack, publication, {})

    await vi.waitFor(() => {
      expect(audioContexts[0].sources).toHaveLength(2)
      expect(observations.at(-1)).toMatchObject({ activeAudioGraphs: 1 })
    })
    client.disconnect()
  })

  test('audio graph初期化失敗後も再接続したtrackを新しいgraphで再生できる', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient((observation) => observations.push(observation))
    await client.connect('ws://127.0.0.1:7880', 'token', 'session-id')
    const room = latestRoom()
    workletFailures.push(new Error('audio worklet initialization failed'))

    room.emit(
      'trackSubscribed',
      { kind: 'audio', mediaStreamTrack: { id: 'failed' } },
      { trackSid: 'TR_failed' },
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
      { trackSid: 'TR_recovered' },
      {},
    )

    await vi.waitFor(() => {
      expect(audioContexts).toHaveLength(2)
      expect(audioContexts[1].sources).toHaveLength(1)
      expect(observations.at(-1)).toMatchObject({
        transport: 'available', audio: 'available', activeAudioGraphs: 1,
      })
    })
    client.disconnect()
  })

  test('barge-inではaudio graphを即時停止し次responseだけで再開する', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient((observation) => observations.push(observation))
    await client.connect('ws://127.0.0.1:7880', 'token', 'session-id')
    const room = latestRoom()
    room.emit(
      'trackSubscribed',
      { kind: 'audio', mediaStreamTrack: {} },
      { trackSid: 'TR_audio' },
      {},
    )
    await vi.waitFor(() => {
      expect(audioContexts).toHaveLength(1)
      expect(audioContexts[0].sources).toHaveLength(1)
    })

    expect(client.stopPlayback('50000000-0000-4000-8000-000000000001', 100)).toBe(0)
    expect(client.stopPlayback('50000000-0000-4000-8000-000000000001', 101)).toBe(0)
    expect(audioContexts[0].sources[0].disconnect).toHaveBeenCalledTimes(1)
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

    await vi.waitFor(() => {
      expect(audioContexts).toHaveLength(2)
      expect(observations.at(-1)).toMatchObject({ activeAudioGraphs: 1 })
    })
    client.disconnect()
  })

  test('再接続中のbarge-in観測はtransport unavailableを維持する', async () => {
    const observations: RoomObservation[] = []
    const client = new LiveKitRoomClient((observation) => observations.push(observation))
    await client.connect('ws://127.0.0.1:7880', 'token', 'session-id')
    const room = latestRoom()

    room.emit('reconnecting')
    client.stopPlayback('50000000-0000-4000-8000-000000000001', 100)

    expect(observations.at(-1)).toMatchObject({
      transport: 'unavailable', control: 'unavailable', audio: 'unavailable',
      speechStartedAtMs: 100,
    })
    client.disconnect()
  })
})
