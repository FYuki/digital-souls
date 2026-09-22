import voiceSessionSchema from '../../../../contracts/voice-session/voice-session.schema.json'
import { compileContractSchema } from '../validation/ajv'
import type { VoiceSessionEvent } from './generated'

const validateVoiceSessionEvent = compileContractSchema(voiceSessionSchema)

export function parseVoiceSessionEvent(value: unknown): VoiceSessionEvent {
  if (!validateVoiceSessionEvent(value)) {
    throw new Error('voice session event does not match protocol 1.0')
  }
  const event = value as VoiceSessionEvent
  if (
    event.text_range !== undefined &&
    event.text_range.start > event.text_range.end
  ) {
    throw new Error('voice session event has an invalid text range')
  }
  return event
}
