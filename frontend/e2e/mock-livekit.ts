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
      createRoom(
        observe: (value: unknown) => void,
        receiveCoreEvent: (value: Record<string, unknown>) => void,
      ) {
        let sessionId = ''
        let conversationId = ''
        let responseSequence = 0
        let activeResponseId: string | null = null
        const emittedUtterances = new Set<string>()
        const controlEvents: Record<string, unknown>[] = []
        let microphoneStream: MediaStream | null = null
        let trackSid = ''
        let inputGeneration = 0
        let inputRevision = 0
        const interruptions: Array<{
          responseId: string
          backendDecisionAtMs: number
          localPlaybackStoppedAtMs: number | null
          cancelConfirmedAtMs: number | null
        }> = []
        const speechBoundary = (type: 'speech_started' | 'speech_stopped', utteranceId: string) => {
          if (!microphoneStream?.getAudioTracks().some(track => track.enabled)) {
            throw new Error('mock speech requires an acknowledged, enabled microphone')
          }
          receiveCoreEvent({
            protocol_version: '2.0', event_id: crypto.randomUUID(),
            type, session_id: sessionId, utterance_id: utteranceId,
            track_sid: trackSid, input_generation: inputGeneration,
            start_sample: 0, detected_sample: 1536, active_end_sample: 1536,
            monotonic_timestamp_ms: Math.floor(performance.now()),
            speaker: {role: 'user', participant_id: '40000000-0000-4000-8000-000000000010'},
            sample_rate: 16000, clock_domain: 'server_monotonic',
          })
          if (type === 'speech_started') {
            const now = performance.now()
            // モック通知起点。実PCM送信・VAD検出の測定証跡ではない。
            window.__voiceChatE2E?.cycles.push({
              fixtureStartedAt: now, sendAt: now,
              audioReceivedAt: null, audioDecodeAt: null, startedAt: null,
              sessionId, utteranceId, responseId: null, conversationId,
              sentBytes: null, receivedBytes: null,
            })
          }
        }
        const cancelResponse = (responseId: string) => {
          receiveCoreEvent({
            type: 'response_cancelled', session_id: sessionId,
            response_id: responseId, reason: 'barge_in',
          })
          const evidence = [...interruptions].reverse().find(row => row.responseId === responseId)
          if (evidence) evidence.cancelConfirmedAtMs = performance.now()
          receiveCoreEvent({
            type: 'response_delta', session_id: sessionId,
            response_id: responseId, text_sequence: 2, text: '破棄対象',
            text_range: {start: responseText.length, end: responseText.length + 4},
          })
          receiveCoreEvent({
            type: 'response_audio_segment', session_id: sessionId,
            response_id: responseId, audio_sequence: 2,
            text_range: {start: responseText.length, end: responseText.length + 4},
          })
          if (activeResponseId === responseId) activeResponseId = null
        }
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
          controlEvents,
          interruptions,
          beginSpeech: () => {
            const id = crypto.randomUUID()
            speechBoundary('speech_started', id)
            return id
          },
          finishSpeech: (id: string) => speechBoundary('speech_stopped', id),
          interruptFromBackend: () => {
            if (activeResponseId === null) throw new Error('no response to interrupt')
            const responseId = activeResponseId
            const utteranceId = crypto.randomUUID()
            speechBoundary('speech_started', utteranceId)
            interruptions.push({
              responseId, backendDecisionAtMs: performance.now(),
              localPlaybackStoppedAtMs: null, cancelConfirmedAtMs: null,
            })
            receiveCoreEvent({
              type: 'turn_decision', session_id: sessionId, utterance_id: utteranceId,
              response_id: responseId, decision: 'take_turn', final: false,
            })
            cancelResponse(responseId)
            speechBoundary('speech_stopped', utteranceId)
          },
          microphoneEnabled: () => microphoneStream?.getAudioTracks().some(track => track.enabled) ?? false,
          resolveTextInput: (status: 'accepted' | 'rejected') => {
            const input = [...controlEvents].reverse().find(event => event.type === 'user_text_submitted')
            if (input === undefined) throw new Error('text input has not arrived')
            receiveCoreEvent({type: 'user_input_result', session_id: sessionId,
              input_event_id: input.event_id, status})
          },
          completeTextResponse: async (text: string) => {
            const input = [...controlEvents].reverse().find(event => event.type === 'user_text_submitted')
            if (input === undefined) throw new Error('text input has not arrived')
            responseSequence += 1
            const responseId = `50000000-0000-4000-8000-${String(responseSequence).padStart(12, '0')}`
            receiveCoreEvent({type: 'user_input_result', session_id: sessionId,
              input_event_id: input.event_id, status: 'accepted', response_id: responseId})
            receiveCoreEvent({type: 'response_started', session_id: sessionId, response_id: responseId,
              source_utterance_ids: [], source_inputs: [{input_id: input.event_id, source: 'text'}]})
            receiveCoreEvent({type: 'response_delta', session_id: sessionId, response_id: responseId,
              text_sequence: 1, text, text_range: {start: 0, end: text.length}})
            await (window as unknown as {__recordMockVoiceTurn: (user: string, assistant: string) => Promise<void>})
              .__recordMockVoiceTurn(String(input.text), text)
            receiveCoreEvent({type: 'response_completed', session_id: sessionId, response_id: responseId,
              last_text_sequence: 1, last_audio_sequence: 0})
            return responseId
          },
          emitLateOutput: (responseId: string) => {
            receiveCoreEvent({type: 'response_delta', session_id: sessionId,
              response_id: responseId, text_sequence: 2, text: '破棄対象'})
            observe({transport: 'available', control: 'available', audio: 'available',
              activeResponseId: responseId, renderedEnergy: 1})
          },
          submitUtterance: async () => {
            const utteranceId = crypto.randomUUID()
            speechBoundary('speech_started', utteranceId)
            speechBoundary('speech_stopped', utteranceId)
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
          async publishMicrophone(stream: MediaStream) {
            microphoneStream = stream
            lifecycle.publishMicrophoneCount += 1
            trackSid = 'TR_mock_' + lifecycle.publishMicrophoneCount
            return trackSid
          },
          async muteMicrophone() {
            lifecycle.muteMicrophoneCount += 1
          },
          stopPlayback(responseId: string) {
            const stoppedAt = performance.now()
            observe({
              transport: 'available', control: 'available', audio: 'unavailable',
              activeResponseId: responseId, localPlaybackStoppedAtMs: stoppedAt,
            })
            const evidence = [...interruptions].reverse().find(row => row.responseId === responseId)
            if (evidence) evidence.localPlaybackStoppedAtMs = stoppedAt
            return 0
          },
          async publishControlEvent(event: Record<string, unknown>) {
            controlEvents.push(event)
            if (event.type === 'speech_started' || event.type === 'speech_stopped') {
              throw new Error('client must not publish authoritative speech boundaries')
            }
            if (event.type === 'audio_input_open_requested') {
              if (event.track_sid !== trackSid) throw new Error('opening an unpublished track')
              inputRevision = Number(event.input_revision)
              inputGeneration += 1
              receiveCoreEvent({
                protocol_version: '2.0', event_id: crypto.randomUUID(),
                type: 'audio_input_opened', session_id: sessionId,
                request_event_id: event.event_id, track_sid: trackSid,
                input_revision: inputRevision, input_generation: inputGeneration,
              })
            }
            if (event.type === 'response_cancel_requested') {
              cancelResponse(String(event.response_id))
            }
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
