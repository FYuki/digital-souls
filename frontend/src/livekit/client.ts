export type TokenResponse = Readonly<{
  session_id: string
  participant_id: string
  room: string
  token: string
  livekit_url: string
  expires_at: string
  reconnect_grace_ms: number
}>

type TokenRequest = {
  protocol_version: '2.0'
  transport_protocol_version: '2.0'
  request_id: string
  wait_for_ready: boolean
  character_id: string
  conversation_id: string
  requested_reconnect_grace_ms: 60000
  session_id?: string
  screen_client_session_id?: string
}

const CHARACTER_ID = 'miori'
const API_PREFIX = '/api'

export class VoicePreparationError extends Error {
  constructor(readonly code: string, stage?: string) {
    const stages: Record<string, string> = {
      tts: '音声合成の準備', vad: '発話検出の準備', stt: '音声認識の準備',
      inference: '応答モデルの準備',
      session: '会話セッションの確保', room: '音声ルームの作成',
      token: '接続情報の発行', transport: '音声サービスへの接続',
      output: '音声出力の準備',
    }
    const reasons: Record<string, string> = {
      tts_not_ready: '音声合成サービスの準備が完了していません',
      tts_voice_missing: '選択した音声が登録されていません',
      tts_config_missing: '音声合成の設定がありません',
      tts_config_invalid: '音声合成の設定を確認してください',
      tts_engine_unsupported: '設定した音声合成方式は利用できません',
      vad_unavailable: '発話検出モデルを利用できません',
      stt_upstream_failed: '音声認識サービスとの通信に失敗しました',
      stt_capacity_exceeded: '音声認識サービスが使用中です',
      stt_inference_timeout: '音声認識の処理がタイムアウトしました',
      stt_preparation_failed: '音声認識の準備に失敗しました',
      inference_timeout: '応答モデルの準備がタイムアウトしました',
      inference_unavailable: '応答モデルのサービスを利用できません',
      inference_model_not_found: '設定した応答モデルが見つかりません',
      inference_rate_limited: '応答モデルのサービスが混雑しています',
      inference_authentication_failed: '応答モデルの認証に失敗しました',
      inference_permission_denied: '応答モデルを利用する権限がありません',
      inference_invalid_response: '応答モデルの準備結果が不正です',
      inference_preparation_failed: '応答モデルの準備に失敗しました',
      inference_invalid_request: '応答モデルの準備設定が不正です',
      inference_unsupported_capability: '応答モデルが必要な準備に対応していません',
      inference_access_denied: '応答モデルの利用が許可されていません',
      inference_provider_error: '応答モデルのサービスでエラーが発生しました',
      inference_cancelled: '応答モデルの準備が取り消されました',
      bootstrap_timeout: '処理がタイムアウトしました',
      request_timeout: 'サーバーへの要求がタイムアウトしました',
      network_error: 'サーバーとの通信に失敗しました',
      invalid_response: 'サーバーの応答形式が不正です',
      bootstrap_conflict: '同じ会話で別の開始処理が進行しています',
      livekit_not_configured: '音声接続の設定がありません',
      protocol_version_mismatch: '画面を再読み込みして更新してください',
      transport_protocol_version_mismatch: '画面を再読み込みして更新してください',
    }
    super(`${stages[stage ?? ''] ?? '音声会話の準備'}：${reasons[code] ?? '処理に失敗しました'}。再試行してください。`)
    this.name = 'VoicePreparationError'
  }
}

const preparationFailure = async (response: Response): Promise<VoicePreparationError> => {
  try {
    const payload: unknown = await response.json()
    if (typeof payload === 'object' && payload !== null && 'detail' in payload
      && typeof payload.detail === 'object' && payload.detail !== null) {
      const detail = payload.detail as Record<string, unknown>
      return new VoicePreparationError(
        typeof detail.code === 'string' ? detail.code : 'bootstrap_failed',
        typeof detail.stage === 'string' ? detail.stage : undefined,
      )
    }
  } catch {
    // proxyのHTMLや外部サービスの例外本文はそのまま表示しない。
  }
  return new VoicePreparationError('bootstrap_failed')
}

const waitForPreparationPoll = (signal?: AbortSignal): Promise<void> => new Promise((resolve, reject) => {
  if (signal?.aborted) { reject(signal.reason); return }
  const abort = () => {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
    reject(signal?.reason)
  }
  const timer = setTimeout(() => {
    signal?.removeEventListener('abort', abort)
    resolve()
  }, 500)
  signal?.addEventListener('abort', abort, {once: true})
})

const requiredString = (value: unknown, field: string): string => {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`LiveKit response field ${field} must be a non-empty string`)
  }
  return value
}

const parseTokenResponse = (value: unknown): TokenResponse => {
  if (typeof value !== 'object' || value === null) {
    throw new Error('LiveKit token response must be an object')
  }
  const response = value as Record<string, unknown>
  const reconnectGraceMs = response.reconnect_grace_ms
  if (!Number.isInteger(reconnectGraceMs) || Number(reconnectGraceMs) < 0) {
    throw new Error('LiveKit response field reconnect_grace_ms must be a non-negative integer')
  }
  const expiresAt = requiredString(response.expires_at, 'expires_at')
  if (Number.isNaN(Date.parse(expiresAt))) {
    throw new Error('LiveKit response field expires_at must be an ISO timestamp')
  }
  return {
    session_id: requiredString(response.session_id, 'session_id'),
    participant_id: requiredString(response.participant_id, 'participant_id'),
    room: requiredString(response.room, 'room'),
    token: requiredString(response.token, 'token'),
    livekit_url: requiredString(response.livekit_url, 'livekit_url'),
    expires_at: expiresAt,
    reconnect_grace_ms: Number(reconnectGraceMs),
  }
}

const parseConversationBinding = (value: unknown): ConversationBinding => {
  if (typeof value !== 'object' || value === null) {
    throw new Error('Conversation response must be an object')
  }
  const response = value as Record<string, unknown>
  return {
    character_id: requiredString(response.character_id, 'character_id'),
    conversation_id: requiredString(response.conversation_id, 'conversation_id'),
  }
}

export const requestLiveKitToken = async (
  characterId: string,
  conversationId: string,
  sessionId?: string,
  screenClientSessionId?: string | null,
  signal?: AbortSignal,
): Promise<TokenResponse> => {
  const body: TokenRequest = {
    protocol_version: '2.0',
    transport_protocol_version: '2.0',
    request_id: crypto.randomUUID(),
    wait_for_ready: sessionId !== undefined,
    character_id: characterId,
    conversation_id: conversationId,
    requested_reconnect_grace_ms: 60000,
  }
  if (sessionId !== undefined) body.session_id = sessionId
  if (screenClientSessionId !== undefined && screenClientSessionId !== null) {
    body.screen_client_session_id = screenClientSessionId
  }
  try {
    for (;;) {
      signal?.throwIfAborted()
      // 個別HTTP要求だけに期限を設け、準備全体の時間では打ち切らない。
      const requestAbort = new AbortController()
      const abort = () => requestAbort.abort(signal?.reason)
      signal?.addEventListener('abort', abort, {once: true})
      const timer = setTimeout(
        () => requestAbort.abort(new DOMException('Request timed out', 'TimeoutError')), 10_000,
      )
      try {
        const response = await fetch(`${API_PREFIX}/voice/livekit/token`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body),
          signal: requestAbort.signal,
        })
        if (response.status === 202 && sessionId === undefined) {
          const pending = await response.json() as Record<string, unknown>
          if (pending.status !== 'preparing' || pending.request_id !== body.request_id) {
            throw new VoicePreparationError('bootstrap_failed')
          }
          await waitForPreparationPoll(signal)
          continue
        }
        if (!response.ok) throw await preparationFailure(response)
        const payload: unknown = await response.json()
        try {
          return parseTokenResponse(payload)
        } catch {
          throw new VoicePreparationError('invalid_response')
        }
      } finally {
        clearTimeout(timer)
        signal?.removeEventListener('abort', abort)
      }
    }
  } catch (error) {
    if (sessionId === undefined) {
      // 応答喪失や画面終了でも開始資源を回収する。失敗時はserver側leaseが回収する。
      void fetch(`${API_PREFIX}/voice/livekit/preparation/cancel`, {
        method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify(body), keepalive: true, signal: AbortSignal.timeout(10_000),
      }).catch(() => undefined)
    }
    if (signal?.aborted) throw signal.reason
    if (error instanceof VoicePreparationError) throw error
    throw new VoicePreparationError(
      error instanceof DOMException && error.name === 'TimeoutError' ? 'request_timeout' : 'network_error',
    )
  }
}

export type ConversationBinding = Readonly<{
  character_id: string
  conversation_id: string
}>

export const createConversation = async (): Promise<ConversationBinding> => {
  const response = await fetch(`${API_PREFIX}/characters/${CHARACTER_ID}/conversations`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error(`Conversation creation failed: ${response.status}`)
  return parseConversationBinding(await response.json() as unknown)
}

export const getInitialToken = async (): Promise<{
  conversationId: string
  token: TokenResponse
}> => {
  const conversation = await createConversation()
  return {
    conversationId: conversation.conversation_id,
    token: await requestLiveKitToken(CHARACTER_ID, conversation.conversation_id),
  }
}
export const getReconnectToken = (
  conversationId: string,
  sessionId: string,
): Promise<TokenResponse> => requestLiveKitToken(
  CHARACTER_ID,
  conversationId,
  sessionId,
)

export const endLiveKitSession = async (sessionId: string): Promise<void> => {
  const response = await fetch(
    `${API_PREFIX}/voice/livekit/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: 'DELETE',
    },
  )
  if (!response.ok && response.status !== 404) {
    throw new Error(`LiveKit session end failed: ${response.status}`)
  }
}
