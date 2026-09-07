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
  | Readonly<{ type: 'control_probe' | 'control_probe_ack'; generation: number; probeId: string }>
  | Readonly<{ type: 'audio_probe_request'; generation: number; probeId: string }>
  | Readonly<{ type: 'audio_probe_ready' | 'audio_probe_complete'; generation: number; probeId: string; trackSid: string }>
  | Readonly<{ type: 'audio_probe_finished'; generation: number; probeId: string; trackSid: string; inputSampleCount: number; capturedSampleCount: number; paddingSampleCount: number }>
  | Readonly<{ type: 'response_audio_finished'; responseId: string; generation: number; inputSampleCount: number; capturedSampleCount: number; paddingSampleCount: number }>
  | Readonly<{ type: 'response_track_ready'; responseId: string; trackSid: string; generation: number }>
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
  | Readonly<{ type: 'control_probe' | 'control_probe_ack'; generation: number; probe_id: string }>
  | Readonly<{ type: 'audio_probe_request'; generation: number; probe_id: string }>
  | Readonly<{ type: 'audio_probe_ready' | 'audio_probe_complete'; generation: number; probe_id: string; track_sid: string }>
  | Readonly<{ type: 'audio_probe_finished'; generation: number; probe_id: string; track_sid: string; input_sample_count: number; captured_sample_count: number; padding_sample_count: number }>
  | Readonly<{ type: 'response_audio_finished'; response_id: string; generation: number; input_sample_count: number; captured_sample_count: number; padding_sample_count: number }>
  | Readonly<{ type: 'response_track_ready'; response_id: string; track_sid: string; generation: number }>
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
    case 'control_probe':
    case 'control_probe_ack':
      return { type: frame.type, generation: frame.generation, probeId: frame.probe_id }
    case 'audio_probe_request':
      return {type: frame.type, generation: frame.generation, probeId: frame.probe_id}
    case 'audio_probe_ready':
    case 'audio_probe_complete':
      return {type: frame.type, generation: frame.generation, probeId: frame.probe_id, trackSid: frame.track_sid}
    case 'audio_probe_finished':
      return {type: frame.type, generation: frame.generation, probeId: frame.probe_id, trackSid: frame.track_sid,
        inputSampleCount: frame.input_sample_count, capturedSampleCount: frame.captured_sample_count,
        paddingSampleCount: frame.padding_sample_count}
    case 'state_sync_request':
      return { type: frame.type, generation: frame.generation }
    case 'response_audio_finished':
      if (frame.input_sample_count + frame.padding_sample_count !== frame.captured_sample_count) throw new Error('source sample conservation failed')
      return {type: frame.type, responseId: frame.response_id, generation: frame.generation,
        inputSampleCount: frame.input_sample_count, capturedSampleCount: frame.captured_sample_count, paddingSampleCount: frame.padding_sample_count}
    case 'response_track_ready':
      return { type: frame.type, responseId: frame.response_id, trackSid: frame.track_sid, generation: frame.generation }
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
