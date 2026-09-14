import Ajv2020 from 'ajv/dist/2020.js'
import addFormats from 'ajv-formats'
import voiceSessionSchema from '../../../../contracts/voice-session/voice-session.schema.json'
import type { VoiceSessionEvent } from './generated'

const ajv = new Ajv2020({ allErrors: true, strict: true })
addFormats(ajv)
const validateVoiceSessionEvent = ajv.compile(voiceSessionSchema)

export function parseVoiceSessionEvent(value: unknown): VoiceSessionEvent {
  if (!validateVoiceSessionEvent(value)) {
    throw new Error('voice session event does not match protocol 2.0')
  }
  const event = value as VoiceSessionEvent
  if ((event.type === 'speech_started' || event.type === 'speech_stopped')
    && !(event.start_sample! <= event.active_end_sample! && event.active_end_sample! <= event.detected_sample!)) {
    throw new Error('voice session event has invalid media positions')
  }
  if (
    event.text_range !== undefined &&
    event.text_range.start > event.text_range.end
  ) {
    throw new Error('voice session event has an invalid text range')
  }
  return event
}
