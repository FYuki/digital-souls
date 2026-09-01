import type { Page } from '@playwright/test'

const SESSION_ID = '20000000-0000-4000-8000-000000000010'
const PARTICIPANT_ID = '40000000-0000-4000-8000-000000000010'

type MockOptions = Readonly<{
  transcript?: string
  response?: string
}>

export const installMockLiveKit = async (
  page: Page,
  options: MockOptions = {},
): Promise<{
  readBindings: (character: string) => string[]
  readTurns: () => Record<string, unknown>[]
}> => {
  const bindings: Array<{ character: string; conversationId: string }> = []
  const turns: Record<string, unknown>[] = []
  const transcript = options.transcript ?? 'テスト音声です'
  const response = options.response ?? 'テスト音声に応答します。'

  await page.exposeFunction('__recordMockVoiceTurn', (
    userContent: string,
    assistantContent: string,
  ) => {
    turns.push({
      kind: 'content',
      turn_id: `90000000-0000-4000-8000-${String(turns.length + 1).padStart(12, '0')}`,
      user_content: userContent,
      assistant_content: assistantContent,
    })
  })

  await page.addInitScript(({ transcriptText, responseText }) => {
    const target = window as unknown as Record<string, unknown>
    const existingPort = target.__digitalSoulsVoiceSessionTestPort as Record<string, unknown> | undefined
    target.__digitalSoulsVoiceSessionTestPort = {
      ...existingPort,
      bindController(controller: unknown) {
        ;(window as unknown as { __mockVoiceController?: unknown })
          .__mockVoiceController = controller
      },
      createRoom(
        observe: (value: unknown) => void,
        receiveCoreEvent: (value: Record<string, unknown>) => void,
      ) {
        let sessionId = ''
        let conversationId = ''
        let responseSequence = 0
        let activeResponseId: string | null = null
        const emittedUtterances = new Set<string>()
        const lifecycle = {
          publishMicrophoneCount: 0,
          muteMicrophoneCount: 0,
          disconnectCount: 0,
        }
        const emitResponse = async (utteranceId: string) => {
          if (emittedUtterances.has(utteranceId)) return
          emittedUtterances.add(utteranceId)
          responseSequence += 1
          const responseId = `50000000-0000-4000-8000-${String(responseSequence).padStart(12, '0')}`
          activeResponseId = responseId
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
          await new Promise((resolve) => setTimeout(resolve, 30))
          if (activeResponseId !== responseId) return
          await (window as unknown as {
            __recordMockVoiceTurn: (
              userContent: string,
              assistantContent: string,
            ) => Promise<void>
          }).__recordMockVoiceTurn(transcriptText, responseText)
          receiveCoreEvent({
            type: 'response_completed', session_id: sessionId,
            response_id: responseId, last_text_sequence: 1,
            last_audio_sequence: 1,
          })
          activeResponseId = null
        }
        ;(window as unknown as { __mockLiveKit?: Record<string, unknown> }).__mockLiveKit = {
          submitUtterance: async () => {
            const utteranceId = crypto.randomUUID()
            await emitResponse(utteranceId)
          },
          beginInterruptibleResponse: () => {
            const utteranceId = crypto.randomUUID()
            responseSequence += 1
            const responseId = `50000000-0000-4000-8000-${String(responseSequence).padStart(12, '0')}`
            activeResponseId = responseId
            receiveCoreEvent({
              type: 'utterance_finalized', session_id: sessionId,
              utterance_id: utteranceId, transcript: '割り込み対象',
              should_response: true,
            })
            receiveCoreEvent({
              type: 'response_started', session_id: sessionId,
              response_id: responseId, source_utterance_ids: [utteranceId],
            })
            receiveCoreEvent({
              type: 'response_delta', session_id: sessionId,
              response_id: responseId, text_sequence: 1,
              text: '長い応答', text_range: { start: 0, end: 4 },
            })
            observe({
              transport: 'available', control: 'available', audio: 'available',
              activeResponseId: responseId, renderedEnergy: 1, renderedSamples: 1,
            })
            return responseId
          },
          disconnect: () => {
            observe({
              transport: 'unavailable', control: 'unavailable', audio: 'unavailable',
            })
          },
          reconnect: () => {
            observe({
              transport: 'available', control: 'available', audio: 'available',
            })
          },
          lifecycle,
        }
        return {
          async connect(_url: string, token: string, connectedSessionId: string) {
            sessionId = connectedSessionId
            conversationId = token.split(':').at(-1) ?? ''
            observe({ transport: 'available', control: 'available', audio: 'unavailable' })
          },
          async publishMicrophone() {
            lifecycle.publishMicrophoneCount += 1
          },
          async muteMicrophone() {
            lifecycle.muteMicrophoneCount += 1
          },
          stopPlayback(responseId: string, speechStartedAtMs: number) {
            const stoppedAt = performance.now()
            observe({
              transport: 'available', control: 'available', audio: 'unavailable',
              activeResponseId: responseId,
              speechStartedAtMs,
              localPlaybackStoppedAtMs: stoppedAt,
            })
            const probe = (window as unknown as { __voiceChatE2E?: {
              localStopAt?: number
              stoppedResponseId?: string
              interruptions: {
                responseId: string
                speechStartedAtMs: number
                localPlaybackStoppedAtMs: number
                cancelConfirmedAtMs: number | null
              }[]
            } }).__voiceChatE2E
            if (probe !== undefined) {
              probe.localStopAt = stoppedAt
              probe.stoppedResponseId = responseId
              probe.interruptions.push({
                responseId,
                speechStartedAtMs,
                localPlaybackStoppedAtMs: stoppedAt,
                cancelConfirmedAtMs: null,
              })
            }
            return 0
          },
          async publishControlEvent(event: Record<string, unknown>) {
            if (event.type === 'response_cancel_requested') {
              const responseId = String(event.response_id)
              const probe = (window as unknown as { __voiceChatE2E?: {
                cancelRequestedAt?: number
                interruptions: {
                  responseId: string
                  cancelConfirmedAtMs: number | null
                }[]
              } }).__voiceChatE2E
              if (probe !== undefined) probe.cancelRequestedAt = performance.now()
              receiveCoreEvent({
                type: 'response_cancelled', session_id: sessionId,
                response_id: responseId, reason: 'barge_in',
              })
              observe({
                transport: 'available', control: 'available', audio: 'unavailable',
                activeResponseId: responseId,
                cancelConfirmedAtMs: performance.now(),
              })
              const interruption = probe?.interruptions.find(
                (candidate) => candidate.responseId === responseId,
              )
              if (interruption !== undefined) {
                interruption.cancelConfirmedAtMs = performance.now()
              }
              receiveCoreEvent({
                type: 'response_delta', session_id: sessionId,
                response_id: responseId, text_sequence: 2,
                text: '破棄対象', text_range: { start: responseText.length, end: responseText.length + 4 },
              })
              receiveCoreEvent({
                type: 'response_audio_segment', session_id: sessionId,
                response_id: responseId, audio_sequence: 2,
                text_range: { start: responseText.length, end: responseText.length + 4 },
              })
              if (activeResponseId === responseId) activeResponseId = null
              return
            }
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
              if (typeof event.response_id === 'string') {
                const responseId = event.response_id
                receiveCoreEvent({
                  type: 'turn_decision', session_id: sessionId,
                  utterance_id: String(event.utterance_id), response_id: responseId,
                  decision: 'take_turn', final: false,
                })
                receiveCoreEvent({
                  type: 'response_cancelled', session_id: sessionId,
                  response_id: responseId, reason: 'barge_in',
                })
                const cancelledAt = performance.now()
                observe({
                  transport: 'available', control: 'available', audio: 'unavailable',
                  activeResponseId: responseId,
                  cancelConfirmedAtMs: cancelledAt,
                })
                const interruption = (window as unknown as { __voiceChatE2E?: {
                  interruptions: {
                    responseId: string
                    cancelConfirmedAtMs: number | null
                  }[]
                } }).__voiceChatE2E?.interruptions.find(
                  (candidate) => candidate.responseId === responseId,
                )
                if (interruption !== undefined) {
                  interruption.cancelConfirmedAtMs = cancelledAt
                }
                receiveCoreEvent({
                  type: 'response_delta', session_id: sessionId,
                  response_id: responseId, text_sequence: 2,
                  text: '破棄対象',
                  text_range: { start: responseText.length, end: responseText.length + 4 },
                })
                if (activeResponseId === responseId) activeResponseId = null
              }
              return
            }
            if (event.type !== 'speech_stopped') return
            await emitResponse(String(event.utterance_id))
          },
          disconnect() {
            lifecycle.disconnectCount += 1
            observe({ transport: 'idle', control: 'unavailable', audio: 'unavailable' })
          },
        }
      },
    }
  }, { transcriptText: transcript, responseText: response })

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
    readTurns: () => turns.map((turn) => ({ ...turn })),
  }
}
