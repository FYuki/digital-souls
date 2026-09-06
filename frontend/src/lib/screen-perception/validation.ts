import Ajv2020 from 'ajv/dist/2020.js'
import addFormats from 'ajv-formats'

import screenPerceptionSchema from '../../../../contracts/perception/screen/screen-perception.schema.json'
import type { ScreenPerceptionEvent } from './generated'

const MAX_IMAGE_PIXELS = 4_194_304
const ajv = new Ajv2020({ allErrors: true, strict: true })
addFormats(ajv)
const validateScreenPerceptionEvent = ajv.compile(screenPerceptionSchema)

export function parseScreenPerceptionEvent(value: unknown): ScreenPerceptionEvent {
  if (!validateScreenPerceptionEvent(value)) {
    throw new Error('screen perception event does not match protocol 1.0')
  }
  const event = value as ScreenPerceptionEvent
  if (
    event.type === 'screen_snapshot_upload_metadata'
    && event.width !== undefined
    && event.height !== undefined
    && event.width * event.height > MAX_IMAGE_PIXELS
  ) {
    throw new Error('screen snapshot exceeds the decoded pixel limit')
  }
  return event
}
