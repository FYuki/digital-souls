import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'

import { AudioWorkletPcmRecorder } from './pcm-worklet-recorder'

const addModule = vi.fn()
const createMediaStreamSource = vi.fn()
const contextClose = vi.fn()
const sourceConnect = vi.fn()
const sourceDisconnect = vi.fn()
const nodeConnect = vi.fn()
const nodeDisconnect = vi.fn()
const portPostMessage = vi.fn()
const stopTrack = vi.fn()
const createObjectURL = vi.fn()
const revokeObjectURL = vi.fn()

let audioContextOptions: AudioContextOptions | undefined

class FakeAudioContext {
  audioWorklet = { addModule }
  destination = {}

  constructor(options: AudioContextOptions) {
    audioContextOptions = options
  }

  createMediaStreamSource = createMediaStreamSource
  close = contextClose
}

class FakeAudioWorkletNode {
  static instances: FakeAudioWorkletNode[] = []

  port: {
    postMessage: typeof portPostMessage
    onmessage: ((event: MessageEvent<unknown>) => void) | null
  } = {
    postMessage: portPostMessage,
    onmessage: null,
  }

  connect = nodeConnect
  disconnect = nodeDisconnect

  constructor(
    readonly context: FakeAudioContext,
    readonly name: string,
    readonly options: AudioWorkletNodeOptions,
  ) {
    FakeAudioWorkletNode.instances.push(this)
  }
}

const source = {
  connect: sourceConnect,
  disconnect: sourceDisconnect,
}

const stream = {
  getTracks: () => [{ stop: stopTrack }],
} as unknown as MediaStream

const latestNode = (): FakeAudioWorkletNode => {
  const node = FakeAudioWorkletNode.instances[FakeAudioWorkletNode.instances.length - 1]

  if (node === undefined) {
    throw new Error('AudioWorkletNode instance is required')
  }

  return node
}

const emitWorkletMessage = (data: unknown) => {
  const onmessage = latestNode().port.onmessage

  if (onmessage === null) {
    throw new Error('AudioWorkletNode onmessage handler is required')
  }

  onmessage(new MessageEvent('message', { data }))
}

const readBytes = (buffer: ArrayBuffer): number[] => Array.from(new Uint8Array(buffer))

describe('AudioWorkletPcmRecorder', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    FakeAudioWorkletNode.instances = []
    audioContextOptions = undefined
    addModule.mockReset()
    createMediaStreamSource.mockReset()
    createMediaStreamSource.mockReturnValue(source)
    contextClose.mockReset()
    contextClose.mockResolvedValue(undefined)
    sourceConnect.mockReset()
    sourceDisconnect.mockReset()
    nodeConnect.mockReset()
    nodeDisconnect.mockReset()
    portPostMessage.mockReset()
    stopTrack.mockReset()
    createObjectURL.mockReset()
    createObjectURL.mockReturnValue('blob:pcm-worklet')
    revokeObjectURL.mockReset()
    addModule.mockResolvedValue(undefined)

    vi.stubGlobal('AudioContext', FakeAudioContext)
    vi.stubGlobal('AudioWorkletNode', FakeAudioWorkletNode)
    vi.stubGlobal('URL', {
      createObjectURL,
      revokeObjectURL,
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  test('should initialize a mono AudioWorklet recording graph', async () => {
    const recorder = new AudioWorkletPcmRecorder()

    await recorder.initialize(stream)

    expect(audioContextOptions).toEqual({ sampleRate: 16000 })
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(addModule).toHaveBeenCalledWith('blob:pcm-worklet')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:pcm-worklet')
    expect(createMediaStreamSource).toHaveBeenCalledWith(stream)

    const node = latestNode()
    expect(node.name).toBe('pcm16-recorder')
    expect(node.options).toEqual({
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    })
    expect(sourceConnect).toHaveBeenCalledWith(node)
    expect(nodeConnect).toHaveBeenCalledTimes(1)
  })

  test('should post start and resolve stopped audio as concatenated PCM chunks', async () => {
    const recorder = new AudioWorkletPcmRecorder()
    await recorder.initialize(stream)

    recorder.start()
    emitWorkletMessage({ type: 'pcm', buffer: new Uint8Array([1, 2]).buffer })
    emitWorkletMessage({ type: 'pcm', buffer: new Uint8Array([3, 4]).buffer })
    const stopped = recorder.stopAndTake()
    emitWorkletMessage({ type: 'stopped' })

    await expect(stopped).resolves.toEqual(new Uint8Array([1, 2, 3, 4]).buffer)
    expect(portPostMessage).toHaveBeenNthCalledWith(1, { type: 'start' })
    expect(portPostMessage).toHaveBeenNthCalledWith(2, { type: 'stop' })
  })

  test('should clear buffered chunks when recording starts again', async () => {
    const recorder = new AudioWorkletPcmRecorder()
    await recorder.initialize(stream)

    recorder.start()
    emitWorkletMessage({ type: 'pcm', buffer: new Uint8Array([9, 9]).buffer })
    recorder.start()
    emitWorkletMessage({ type: 'pcm', buffer: new Uint8Array([5, 6]).buffer })
    const stopped = recorder.stopAndTake()
    emitWorkletMessage({ type: 'stopped' })

    expect(readBytes(await stopped)).toEqual([5, 6])
  })

  test('should reject overlapping stop requests', async () => {
    const recorder = new AudioWorkletPcmRecorder()
    await recorder.initialize(stream)

    const stopped = recorder.stopAndTake()

    expect(() => recorder.stopAndTake()).toThrow('PCM recorder is already stopping')
    emitWorkletMessage({ type: 'stopped' })
    await stopped
  })

  test('should fail fast when worklet messages do not match the recorder contract', async () => {
    const recorder = new AudioWorkletPcmRecorder()
    await recorder.initialize(stream)

    expect(() => emitWorkletMessage({ type: 'pcm', buffer: 'not-array-buffer' })).toThrow(
      'PCM recorder message shape is invalid',
    )
  })

  test('should release the graph, media tracks, context, and buffered state on close', async () => {
    const recorder = new AudioWorkletPcmRecorder()
    await recorder.initialize(stream)

    await recorder.close()

    expect(nodeDisconnect).toHaveBeenCalledTimes(1)
    expect(sourceDisconnect).toHaveBeenCalledTimes(1)
    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(contextClose).toHaveBeenCalledTimes(1)
    expect(() => recorder.start()).toThrow('PCM recorder is not initialized')
  })

  test('should release the media tracks and context when initialization fails', async () => {
    const recorder = new AudioWorkletPcmRecorder()
    const error = new Error('worklet module failed')
    addModule.mockRejectedValueOnce(error)

    await expect(recorder.initialize(stream)).rejects.toThrow(error)

    expect(stopTrack).toHaveBeenCalledTimes(1)
    expect(contextClose).toHaveBeenCalledTimes(1)
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:pcm-worklet')
    expect(() => recorder.start()).toThrow('PCM recorder is not initialized')
  })
})
