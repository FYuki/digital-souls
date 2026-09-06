import { parseChatResponseBody, type ChatResponse } from '../chat/client'
import type {
  ActualSurface,
  RoutingDisclosure,
  SessionStarted,
  SnapshotUploadAccepted,
} from './generated'
import type { CapturedScreenSnapshot } from './capture'
import { parseScreenPerceptionEvent } from './validation'

const ROOT = '/api/perception/screen'

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === 'object' && value !== null
)

const parseEventResponse = async (response: Response) => {
  if (!response.ok) throw new Error(`Screen perception request failed with status ${response.status}`)
  return parseScreenPerceptionEvent(await response.json())
}

export const fetchScreenRouting = async (): Promise<RoutingDisclosure> => {
  const event = await parseEventResponse(await fetch(`${ROOT}/routing`, {
    credentials: 'same-origin',
  }))
  if (event.type !== 'screen_routing_disclosed') throw new Error('Screen routing response is invalid')
  return event
}

export const startScreenSession = async (input: {
  routing: RoutingDisclosure
  generation: number
  characterId: string
  conversationId: string
  requestedSurface: ActualSurface
  actualSurface: ActualSurface
  cloudVisionConsent: boolean
  cloudDerivedChatConsent: boolean
}): Promise<SessionStarted> => {
  const event = await parseEventResponse(await fetch(`${ROOT}/sessions`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      protocol_version: '1.0',
      type: 'screen_session_start_requested',
      event_id: crypto.randomUUID(),
      client_session_id: input.routing.client_session_id,
      generation: input.generation,
      character_id: input.characterId,
      conversation_id: input.conversationId,
      requested_surface: input.requestedSurface,
      actual_surface: input.actualSurface,
      routing_revision: input.routing.routing_revision,
      cloud_consent: {
        cloud_vision: input.cloudVisionConsent,
        cloud_derived_chat: input.cloudDerivedChatConsent,
      },
    }),
  }))
  if (event.type !== 'screen_session_started') throw new Error('Screen session response is invalid')
  return event
}

export const heartbeatScreenSession = async (
  session: SessionStarted,
): Promise<void> => {
  const event = await parseEventResponse(await fetch(
    `${ROOT}/sessions/${encodeURIComponent(session.screen_session_id)}/heartbeat`,
    {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        protocol_version: '1.0',
        type: 'screen_session_heartbeat',
        event_id: crypto.randomUUID(),
        screen_session_id: session.screen_session_id,
        client_session_id: session.client_session_id,
        generation: session.generation,
      }),
    },
  ))
  if (event.type !== 'screen_session_heartbeat_accepted') {
    throw new Error('Screen heartbeat response is invalid')
  }
}

export const revokeScreenSession = async (
  session: SessionStarted,
  reason: 'user_off' | 'target_change' | 'conversation_change' | 'character_change'
    | 'consent_revoked' | 'capture_ended' | 'pagehide' | 'backend_disconnect',
): Promise<void> => {
  const event = await parseEventResponse(await fetch(
    `${ROOT}/sessions/${encodeURIComponent(session.screen_session_id)}`,
    {
      method: 'DELETE',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        protocol_version: '1.0',
        type: 'screen_session_revoke_requested',
        event_id: crypto.randomUUID(),
        screen_session_id: session.screen_session_id,
        client_session_id: session.client_session_id,
        generation: session.generation,
        reason,
      }),
      keepalive: reason === 'pagehide',
    },
  ))
  if (event.type !== 'screen_session_revoked') throw new Error('Screen revoke response is invalid')
}

export type ScreenUploadResult = {
  accepted: SnapshotUploadAccepted | null
  chat: ChatResponse | null
}

export const uploadScreenSnapshot = async (
  snapshot: CapturedScreenSnapshot,
  expectedCharacter: string,
): Promise<ScreenUploadResult> => {
  const metadata = snapshot.metadata
  const response = await fetch(
    `${ROOT}/requests/${encodeURIComponent(metadata.request_id)}/image`,
    {
      method: 'PUT',
      credentials: 'same-origin',
      headers: {
        'Content-Type': metadata.mime_type,
        'X-Screen-Protocol-Version': metadata.protocol_version,
        'X-Screen-Event-Id': metadata.event_id,
        'X-Screen-Session-Id': metadata.screen_session_id,
        'X-Screen-Client-Session-Id': metadata.client_session_id,
        'X-Screen-Generation': String(metadata.generation),
        'X-Screen-Turn-Id': metadata.turn_id,
        'X-Screen-Image-Id': metadata.image_id,
        'X-Screen-Surface': metadata.actual_surface,
        'X-Screen-Captured-At': metadata.captured_at,
        'X-Screen-Width': String(metadata.width),
        'X-Screen-Height': String(metadata.height),
      },
      body: snapshot.blob,
    },
  )
  if (!response.ok) throw new Error(`Screen upload failed with status ${response.status}`)
  const body: unknown = await response.json()
  if (isRecord(body) && 'upload' in body) {
    const accepted = parseScreenPerceptionEvent(body.upload)
    if (accepted.type !== 'screen_snapshot_upload_accepted') {
      throw new Error('Screen upload response is invalid')
    }
    return {
      accepted,
      chat: parseChatResponseBody(body, expectedCharacter),
    }
  }
  const accepted = parseScreenPerceptionEvent(body)
  if (accepted.type !== 'screen_snapshot_upload_accepted') {
    throw new Error('Screen upload response is invalid')
  }
  return { accepted, chat: null }
}

export const reportScreenCaptureFailure = async (
  request: import('./generated').SnapshotRequested,
  reasonCode: import('./generated').ReasonCode,
  expectedCharacter: string,
): Promise<ChatResponse | null> => {
  const event = {
    protocol_version: '1.0' as const,
    type: 'screen_error' as const,
    event_id: crypto.randomUUID(),
    screen_session_id: request.screen_session_id,
    generation: request.generation,
    request_id: request.request_id,
    turn_id: request.turn_id,
    stage: 'capture' as const,
    reason_code: reasonCode,
    recoverable: true,
  }
  parseScreenPerceptionEvent(event)
  const response = await fetch(
    `${ROOT}/requests/${encodeURIComponent(request.request_id)}/failure`,
    {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(event),
    },
  )
  if (!response.ok) throw new Error(`Screen failure report failed with status ${response.status}`)
  const body: unknown = await response.json()
  if (isRecord(body) && 'error' in body) {
    const returned = parseScreenPerceptionEvent(body.error)
    if (returned.type !== 'screen_error') throw new Error('Screen failure response is invalid')
    return parseChatResponseBody(body, expectedCharacter)
  }
  const returned = parseScreenPerceptionEvent(body)
  if (returned.type !== 'screen_error') throw new Error('Screen failure response is invalid')
  return null
}
