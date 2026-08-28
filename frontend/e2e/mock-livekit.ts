import type { Page } from '@playwright/test'

const SESSION_ID = '20000000-0000-4000-8000-000000000010'
const PARTICIPANT_ID = '40000000-0000-4000-8000-000000000010'
const RESPONSE_ID = '50000000-0000-4000-8000-000000000010'

type MockOptions = Readonly<{
  transcript?: string
  response?: string
}>

export const installMockLiveKit = async (
  page: Page,
  options: MockOptions = {},
): Promise<{ readBindings: (character: string) => string[] }> => {
  const bindings: Array<{ character: string; conversationId: string }> = []
  const transcript = options.transcript ?? 'テスト音声です'
  const response = options.response ?? 'テスト音声に応答します。'

  await page.addInitScript(({ transcriptText, responseText, responseId }) => {
    const target = window as unknown as Record<string, unknown>
    const existingPort = target.__digitalSoulsVoiceSessionTestPort as Record<string, unknown> | undefined
    target.__digitalSoulsVoiceSessionTestPort = {
      ...existingPort,
      createRoom(
        observe: (value: unknown) => void,
        receiveCoreEvent: (value: Record<string, unknown>) => void,
      ) {
        let sessionId = ''
        let conversationId = ''
        const emittedUtterances = new Set<string>()
        const emitResponse = (utteranceId: string) => {
          if (emittedUtterances.has(utteranceId)) return
          emittedUtterances.add(utteranceId)
          receiveCoreEvent({
            type: 'utterance_finalized', session_id: sessionId,
            utterance_id: utteranceId, transcript: transcriptText,
            should_response: true,
          })
          receiveCoreEvent({
            type: 'response_started', session_id: sessionId,
            response_id: responseId, source_utterance_ids: [utteranceId],
          })
          receiveCoreEvent({
            type: 'response_delta', session_id: sessionId,
            response_id: responseId, text_sequence: 1,
            text: responseText, text_range: { start: 0, end: responseText.length },
          })
          const probe = (window as unknown as { __voiceChatE2E?: {
            cycles: Record<string, unknown>[]
            frameOrder: string[]
          } }).__voiceChatE2E
          probe?.frameOrder.push('text-delta')
          receiveCoreEvent({
            type: 'response_audio_segment', session_id: sessionId,
            response_id: responseId, audio_sequence: 1,
            text_range: { start: 0, end: responseText.length },
          })
          probe?.frameOrder.push('audio')
          const cycle = probe?.cycles.at(-1)
          if (cycle !== undefined) {
            const now = performance.now()
            cycle.responseId = responseId
            cycle.audioReceivedAt = now
            cycle.audioDecodeAt = now
            cycle.startedAt = now
            cycle.receivedBytes = 1
          }
        }
        return {
          async connect(_url: string, token: string, connectedSessionId: string) {
            sessionId = connectedSessionId
            conversationId = token.split(':').at(-1) ?? ''
            observe({ transport: 'available', control: 'available', audio: 'unavailable' })
          },
          async publishMicrophone() {},
          async muteMicrophone() {},
          async publishControlEvent(event: Record<string, unknown>) {
            if (event.type === 'speech_started') {
              const probe = (window as unknown as { __voiceChatE2E?: {
                cycles: Record<string, unknown>[]
              } }).__voiceChatE2E
              probe?.cycles.push({
                fixtureStartedAt: performance.now(),
                sendAt: performance.now(),
                audioReceivedAt: null,
                audioDecodeAt: null,
                startedAt: null,
                sessionId,
                utteranceId: String(event.utterance_id),
                responseId: null,
                conversationId,
                sentBytes: 1,
                receivedBytes: null,
              })
              setTimeout(() => emitResponse(String(event.utterance_id)), 20)
              return
            }
            if (event.type !== 'speech_stopped') return
            emitResponse(String(event.utterance_id))
          },
          disconnect() {
            observe({ transport: 'idle', control: 'unavailable', audio: 'unavailable' })
          },
        }
      },
    }
  }, { transcriptText: transcript, responseText: response, responseId: RESPONSE_ID })

  await page.route('**/api/voice/livekit/token', async (route) => {
    const body = route.request().postDataJSON() as Record<string, string>
    bindings.push({ character: body.character_id, conversationId: body.conversation_id })
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        session_id: SESSION_ID,
        participant_id: PARTICIPANT_ID,
        room: `mock-${body.conversation_id}`,
        token: `mock-token:${body.character_id}:${body.conversation_id}`,
        livekit_url: 'ws://mock-livekit.invalid',
        expires_at: '2026-08-28T12:00:00.000Z',
        reconnect_grace_ms: 60_000,
      }),
    })
  })
  await page.route('**/api/voice/livekit/sessions/**', async (route) => {
    await route.fulfill({ status: 204, body: '' })
  })

  return {
    readBindings: (character) => bindings
      .filter((binding) => binding.character === character)
      .map((binding) => binding.conversationId),
  }
}
