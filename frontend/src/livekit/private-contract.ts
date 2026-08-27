import Ajv2020 from 'ajv/dist/2020.js'
import addFormats from 'ajv-formats'

import privateSchema from '../../../contracts/livekit-transport/livekit-transport.schema.json'

type TerminalOutcome = Readonly<{
  type: 'response_interrupted'
  sessionId: string
  responseId: string
  confirmedAudioSequence: number
}>

type PrivateFrame =
  | Readonly<{
    type: 'authoritative_state'
    generation: number
    sessionPhase: 'available' | 'unavailable' | 'ended'
    terminalOutcomes: ReadonlyArray<TerminalOutcome>
  }>
  | Readonly<{ type: 'ack'; eventId: string; generation: number }>
  | Readonly<{ type: 'state_sync_request'; generation: number }>
  | Readonly<{
    type: 'logical_audio_segment'
    responseId: string
    audioSequence: number
    generation: number
    pcmSampleCount: number
  }>
  | Readonly<{
    type: 'microphone_observation'
    generation: number
    frameCount: number
    sampleCount: number
    elapsedMs: number
    missingFrames: number
  }>

type TerminalOutcomeWire = Readonly<{
  type: 'response_interrupted'
  session_id: string
  response_id: string
  confirmed_audio_sequence: number
}>

type PrivateFrameWire =
  | Readonly<{
    type: 'authoritative_state'
    generation: number
    session_phase: 'available' | 'unavailable' | 'ended'
    terminal_outcomes: ReadonlyArray<TerminalOutcomeWire>
  }>
  | Readonly<{ type: 'ack'; event_id: string; generation: number }>
  | Readonly<{ type: 'state_sync_request'; generation: number }>
  | Readonly<{
    type: 'logical_audio_segment'
    response_id: string
    audio_sequence: number
    generation: number
    pcm_sample_count: number
  }>
  | Readonly<{
    type: 'microphone_observation'
    generation: number
    frame_count: number
    sample_count: number
    elapsed_ms: number
    missing_frames: number
  }>

const ajv = new Ajv2020({ allErrors: true, strict: true })
addFormats(ajv)
const validatePrivateFrame = ajv.compile(privateSchema)

export function parsePrivateFrame(value: unknown): PrivateFrame {
  if (!validatePrivateFrame(value)) {
    throw new Error('LiveKit private frame does not match protocol 1.0')
  }
  const frame = value as PrivateFrameWire
  switch (frame.type) {
    case 'authoritative_state':
      return {
        type: frame.type,
        generation: frame.generation,
        sessionPhase: frame.session_phase,
        terminalOutcomes: frame.terminal_outcomes.map((outcome) => ({
          type: outcome.type,
          sessionId: outcome.session_id,
          responseId: outcome.response_id,
          confirmedAudioSequence: outcome.confirmed_audio_sequence,
        })),
      }
    case 'ack':
      return {
        type: frame.type,
        eventId: frame.event_id,
        generation: frame.generation,
      }
    case 'state_sync_request':
      return { type: frame.type, generation: frame.generation }
    case 'logical_audio_segment':
      return {
        type: frame.type,
        responseId: frame.response_id,
        audioSequence: frame.audio_sequence,
        generation: frame.generation,
        pcmSampleCount: frame.pcm_sample_count,
      }
    case 'microphone_observation':
      return {
        type: frame.type,
        generation: frame.generation,
        frameCount: frame.frame_count,
        sampleCount: frame.sample_count,
        elapsedMs: frame.elapsed_ms,
        missingFrames: frame.missing_frames,
      }
    default: {
      const unsupported: never = frame
      throw new Error(`Unsupported LiveKit private frame: ${String(unsupported)}`)
    }
  }
}

export function decodePrivateFrame(payload: Uint8Array): PrivateFrame {
  return parsePrivateFrame(
    JSON.parse(new TextDecoder().decode(payload)) as unknown,
  )
}
