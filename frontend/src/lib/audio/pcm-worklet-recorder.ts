import { TARGET_SAMPLE_RATE } from './pcm-format'

const PCM_WORKLET_PROCESSOR_NAME = 'pcm16-recorder'

const PCM_WORKLET_SOURCE = `
class Pcm16RecorderProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    this.isRecording = false
    this.inputSampleIndex = 0
    this.outputSampleIndex = 0
    this.port.onmessage = (event) => {
      if (event.data.type === 'start') {
        this.isRecording = true
        this.inputSampleIndex = 0
        this.outputSampleIndex = 0
        return
      }

      if (event.data.type === 'stop') {
        this.isRecording = false
        this.port.postMessage({ type: 'stopped' })
      }
    }
  }

  process(inputs, outputs) {
    const output = outputs[0]
    if (output !== undefined) {
      for (const channel of output) {
        channel.fill(0)
      }
    }

    const input = inputs[0]
    const channel = input?.[0]
    if (!this.isRecording || channel === undefined) {
      return true
    }

    const pcmSamples = []
    for (let index = 0; index < channel.length; index += 1) {
      const sourceIndex = this.inputSampleIndex + index
      while ((this.outputSampleIndex * sampleRate) / ${TARGET_SAMPLE_RATE} <= sourceIndex) {
        pcmSamples.push(this.toInt16(channel[index]))
        this.outputSampleIndex += 1
      }
    }

    this.inputSampleIndex += channel.length

    if (pcmSamples.length > 0) {
      const buffer = new ArrayBuffer(pcmSamples.length * 2)
      const view = new DataView(buffer)
      for (let index = 0; index < pcmSamples.length; index += 1) {
        view.setInt16(index * 2, pcmSamples[index], true)
      }
      this.port.postMessage({ type: 'pcm', buffer }, [buffer])
    }

    return true
  }

  toInt16(sample) {
    const clamped = Math.max(-1, Math.min(1, sample))
    if (clamped < 0) {
      return Math.round(clamped * 32768)
    }
    return Math.floor(clamped * 32767)
  }
}

registerProcessor('${PCM_WORKLET_PROCESSOR_NAME}', Pcm16RecorderProcessor)
`

type PcmChunkMessage = {
  type: 'pcm'
  buffer: ArrayBuffer
}

type RecorderStoppedMessage = {
  type: 'stopped'
}

type RecorderMessage = PcmChunkMessage | RecorderStoppedMessage

const isRecord = (value: unknown): value is Record<string, unknown> => {
  return typeof value === 'object' && value !== null
}

const isRecorderMessage = (value: unknown): value is RecorderMessage => {
  if (!isRecord(value)) {
    return false
  }

  if (value.type === 'stopped') {
    return true
  }

  return value.type === 'pcm' && value.buffer instanceof ArrayBuffer
}

const createWorkletModuleUrl = (): string => {
  const blob = new Blob([PCM_WORKLET_SOURCE], { type: 'application/javascript' })
  return URL.createObjectURL(blob)
}

const concatenateChunks = (chunks: ArrayBuffer[]): ArrayBuffer => {
  const totalLength = chunks.reduce((length, chunk) => length + chunk.byteLength, 0)
  const merged = new Uint8Array(totalLength)
  let offset = 0

  for (const chunk of chunks) {
    merged.set(new Uint8Array(chunk), offset)
    offset += chunk.byteLength
  }

  return merged.buffer
}

export class AudioWorkletPcmRecorder {
  #context: AudioContext | null = null
  #stream: MediaStream | null = null
  #source: MediaStreamAudioSourceNode | null = null
  #node: AudioWorkletNode | null = null
  #chunks: ArrayBuffer[] = []
  #stopResolver: (() => void) | null = null

  async initialize(stream: MediaStream): Promise<void> {
    if (this.#node !== null) {
      return
    }

    const context = new AudioContext({ sampleRate: TARGET_SAMPLE_RATE })
    const moduleUrl = createWorkletModuleUrl()
    let source: MediaStreamAudioSourceNode | null = null
    let node: AudioWorkletNode | null = null

    try {
      await context.audioWorklet.addModule(moduleUrl)
      source = context.createMediaStreamSource(stream)
      node = new AudioWorkletNode(context, PCM_WORKLET_PROCESSOR_NAME, {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      })
      node.port.onmessage = (event: MessageEvent<unknown>) => {
        this.handleMessage(event.data)
      }
      source.connect(node)
      node.connect(context.destination)
    } catch (error) {
      node?.disconnect()
      source?.disconnect()
      stream.getTracks().forEach((track) => {
        track.stop()
      })
      await context.close()
      throw error
    } finally {
      URL.revokeObjectURL(moduleUrl)
    }

    this.#context = context
    this.#stream = stream
    this.#source = source
    this.#node = node
  }

  start(): void {
    this.getNode().port.postMessage({ type: 'start' })
    this.#chunks = []
  }

  stopAndTake(): Promise<ArrayBuffer> {
    const node = this.getNode()

    if (this.#stopResolver !== null) {
      throw new Error('PCM recorder is already stopping')
    }

    return new Promise((resolve) => {
      this.#stopResolver = () => {
        resolve(concatenateChunks(this.#chunks))
        this.#chunks = []
        this.#stopResolver = null
      }
      node.port.postMessage({ type: 'stop' })
    })
  }

  async close(): Promise<void> {
    this.#node?.disconnect()
    this.#source?.disconnect()
    this.#stream?.getTracks().forEach((track) => {
      track.stop()
    })

    if (this.#context !== null) {
      await this.#context.close()
    }

    this.#node = null
    this.#source = null
    this.#stream = null
    this.#context = null
    this.#chunks = []
    this.#stopResolver = null
  }

  private getNode(): AudioWorkletNode {
    if (this.#node === null) {
      throw new Error('PCM recorder is not initialized')
    }

    return this.#node
  }

  private handleMessage(data: unknown): void {
    if (!isRecorderMessage(data)) {
      throw new Error('PCM recorder message shape is invalid')
    }

    if (data.type === 'pcm') {
      this.#chunks.push(data.buffer)
      return
    }

    if (this.#stopResolver === null) {
      throw new Error('PCM recorder stopped without a pending request')
    }

    this.#stopResolver()
  }
}
