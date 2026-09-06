<script lang="ts">
  import { onDestroy, onMount } from 'svelte'

  import type {
    ActualSurface,
    RecognitionState,
    RoutingDisclosure,
    SessionStarted,
    SnapshotRequested,
  } from './screen-perception/generated'
  import {
    BrowserScreenCaptureController,
    screenCaptureFailureReason,
    type AuthorizedSnapshotRequest,
    type CapturedScreenSnapshot,
    type ScreenCaptureState,
  } from './screen-perception/capture'
  import {
    fetchScreenRouting,
    heartbeatScreenSession,
    reportScreenCaptureFailure,
    revokeScreenSession,
    startScreenSession,
    uploadScreenSnapshot,
    type ScreenUploadResult,
  } from './screen-perception/client'

  export let characterId: string
  export let conversationId: string | null
  export let disabled = false
  export let coreConnected = false
  export let requestSnapshot: (() => Promise<AuthorizedSnapshotRequest | null>) | null = null
  export let onSnapshotCaptured: ((snapshot: CapturedScreenSnapshot) => Promise<void>) | null = null
  /** #216の会話runtimeが参照要否を判定している間だけtrueにする。 */
  export let referenceDecisionActive = false
  export let onReferenceAvailabilityChanged: (available: boolean) => void = () => undefined

  let preview: HTMLVideoElement
  let controller: BrowserScreenCaptureController | null = null
  let state: ScreenCaptureState = {
    generation: 0,
    captureState: 'off',
    recognitionState: 'idle',
    reasonCode: null,
    requestedSurface: 'monitor',
    actualSurface: null,
    targetLabel: null,
    trackMuted: false,
    lastRecognizedCaptureAt: null,
  }
  let requestedSurface: ActualSurface = 'monitor'
  let mounted = false
  let previousCharacterId = characterId
  let previousConversationId = conversationId
  let previousCoreConnected = coreConnected
  let operationButton: HTMLButtonElement | null = null
  let requestingAuthorization = false
  let routing: RoutingDisclosure | null = null
  let backendSession: SessionStarted | null = null
  let heartbeatTimer: number | null = null
  let backendSessionStarting = false
  let cloudVisionConsent = false
  let cloudDerivedChatConsent = false

  const captureLabels = {
    off: '停止中',
    selecting: '選択中',
    active: '共有中',
    unavailable: '取得不能',
    unsupported: '非対応',
  } as const
  const recognitionLabels: Record<RecognitionState, string> = {
    idle: '待機',
    snapshot_requested: '参照要求を確認中',
    capturing: '静止画を取得中',
    uploading: '送信中',
    recognizing: '認識中',
    composing: '回答処理中',
    succeeded: '完了',
    failed: '失敗',
  }
  const surfaceLabels: Record<ActualSurface, string> = {
    monitor: 'モニター',
    window: 'ウィンドウ',
    browser: 'ブラウザタブ',
  }
  const reasonLabels = {
    api_unavailable: 'このブラウザでは画面共有APIを利用できません。',
    insecure_context: '安全な接続ではないため画面共有を利用できません。',
    transient_activation_required: 'ボタンを押して、もう一度対象を選択してください。',
    capture_not_allowed: '画面共有が許可されませんでした。ブラウザまたは管理設定と選択内容を確認してください。',
    no_capture_source: '共有できる画面が見つかりませんでした。',
    capture_os_error: '画面共有を開始できませんでした。',
    picker_cancelled: '画面の選択を取り消しました。',
    surface_mismatch: '希望と異なる種類が選ばれたため共有しませんでした。',
    surface_unknown: '共有対象の種類を確認できなかったため共有しませんでした。',
    video_track_invalid: '共有映像を利用できません。',
    frame_unavailable: '現在の画面フレームを取得できません。',
    image_type_invalid: '静止画の形式を利用できません。',
    image_too_large: '静止画を安全なサイズまで縮小できません。',
    image_decode_failed: '静止画を作成できません。',
    session_not_found: '画面参照セッションを確認できません。',
    session_revoked: '画面参照セッションは終了しています。',
    session_expired: '画面参照セッションの期限が切れました。',
    generation_mismatch: '共有対象が変更されたため、この参照要求を中止しました。',
    context_mismatch: '会話が変更されたため、この参照要求を中止しました。',
    request_not_found: '画面参照要求を確認できません。',
    request_expired: '画面参照要求の期限が切れました。',
    duplicate_request: '同じ画面参照要求はすでに処理済みです。',
    cloud_consent_required: '画面情報のクラウド送信には確認が必要です。',
    routing_changed: '送信先が変更されたため、画面共有を停止しました。',
    vision_unconfigured: '画面認識はまだ設定されていません。',
    vision_unsupported: '現在の画面認識設定では画像を扱えません。',
    vision_timeout: '画面認識が時間内に完了しませんでした。',
    vision_unavailable: '画面認識を利用できません。',
    vision_invalid_response: '画面認識の結果を利用できません。',
    request_cancelled: '画面参照を中止しました。',
    backend_unavailable: '画面参照のサーバー連携は準備中です。共有画像は送信されません。',
  } as const

  $: sharing = state.captureState === 'active' || state.captureState === 'selecting'
  $: busy = requestingAuthorization
    || referenceDecisionActive
    || state.captureState === 'selecting'
    || ['snapshot_requested', 'capturing', 'uploading', 'recognizing', 'composing'].includes(state.recognitionState)
  $: snapshotAvailable = state.captureState === 'active'
    && requestSnapshot !== null
    && onSnapshotCaptured !== null
    && conversationId !== null
    && !disabled
    && !busy
  $: captureStatus = captureLabels[state.captureState]
  $: currentTarget = state.targetLabel === null
    ? state.captureState === 'selecting' ? '選択中' : '未選択'
    : `${state.actualSurface === null ? '' : `${surfaceLabels[state.actualSurface]}・`}${state.targetLabel}`
  $: recognitionStatus = referenceDecisionActive
    ? '参照が必要か確認中'
    : requestingAuthorization
      ? recognitionLabels.snapshot_requested
      : state.recognitionState === 'idle'
        && backendSession !== null
        && state.captureState === 'active'
        ? '参照可能'
        : recognitionLabels[state.recognitionState]
  $: referenceAvailableValue = backendSession !== null
    && state.captureState === 'active'
    && !busy
  $: if (mounted) onReferenceAvailabilityChanged(referenceAvailableValue)

  onMount(() => {
    controller = new BrowserScreenCaptureController(preview, observeCaptureState)
    mounted = true
    void fetchScreenRouting().then((value) => {
      if (!mounted) return
      routing = value
      void ensureBackendSession()
    }).catch(() => undefined)
    const handlePageHide = () => stopAndRevoke('pagehide')
    window.addEventListener('pagehide', handlePageHide)
    return () => window.removeEventListener('pagehide', handlePageHide)
  })

  onDestroy(() => {
    mounted = false
    stopAndRevoke('pagehide')
    controller = null
  })

  $: syncCaptureContext(characterId, conversationId)
  $: syncCoreConnection(coreConnected)

  function syncCaptureContext(nextCharacterId: string, nextConversationId: string | null) {
    if (!mounted) return
    if (nextCharacterId !== previousCharacterId) {
      previousCharacterId = nextCharacterId
      previousConversationId = nextConversationId
      stopAndRevoke('character_change')
    } else if (nextConversationId !== previousConversationId) {
      previousConversationId = nextConversationId
      stopAndRevoke('conversation_change')
    }
  }

  function syncCoreConnection(nextConnected: boolean) {
    if (!mounted) return
    if (previousCoreConnected && !nextConnected) stopAndRevoke('backend_disconnect')
    previousCoreConnected = nextConnected
  }

  const restoreFocus = () => {
    operationButton?.focus()
  }

  const selectTarget = async () => {
    if (disabled || conversationId === null || controller === null) return
    const selectedCharacterId = characterId
    const selectedConversationId = conversationId
    await controller.selectSurface(requestedSurface)
    if (selectedCharacterId !== characterId || selectedConversationId !== conversationId) {
      stopAndRevoke(selectedCharacterId !== characterId ? 'character_change' : 'conversation_change')
    } else {
      void ensureBackendSession()
    }
    restoreFocus()
  }

  const stopSharing = () => {
    stopAndRevoke('user_off')
    restoreFocus()
  }

  const changeTarget = async () => {
    stopAndRevoke('target_change')
    await selectTarget()
  }

  const captureForCurrentTurn = async () => {
    if (!snapshotAvailable || controller === null || requestSnapshot === null || onSnapshotCaptured === null) return
    let authorization: AuthorizedSnapshotRequest | null
    requestingAuthorization = true
    try {
      authorization = await requestSnapshot()
    } catch {
      controller.failRecognition('backend_unavailable')
      return
    } finally {
      requestingAuthorization = false
    }
    if (authorization === null) {
      controller.failRecognition('backend_unavailable')
      return
    }
    let snapshot: CapturedScreenSnapshot
    try {
      snapshot = await controller.captureSnapshot(authorization)
    } catch (error) {
      controller.failRecognition(screenCaptureFailureReason(error))
      return
    }
    try {
      await onSnapshotCaptured(snapshot)
    } catch {
      controller.failRecognition('backend_unavailable')
    }
  }

  function observeCaptureState(next: ScreenCaptureState) {
    const ended = state.captureState === 'active' && next.captureState === 'unavailable'
    state = next
    if (ended) void revokeBackend('capture_ended')
  }

  function stopHeartbeat() {
    if (heartbeatTimer !== null) window.clearInterval(heartbeatTimer)
    heartbeatTimer = null
  }

  async function revokeBackend(
    reason: 'user_off' | 'target_change' | 'conversation_change' | 'character_change'
      | 'consent_revoked' | 'capture_ended' | 'pagehide' | 'backend_disconnect',
  ) {
    const session = backendSession
    backendSession = null
    stopHeartbeat()
    if (session === null) return
    await revokeScreenSession(session, reason).catch(() => undefined)
  }

  function stopAndRevoke(
    reason: 'user_off' | 'target_change' | 'conversation_change' | 'character_change'
      | 'consent_revoked' | 'capture_ended' | 'pagehide' | 'backend_disconnect',
  ) {
    controller?.stop(reason)
    void revokeBackend(reason)
  }

  function consentSatisfied(): boolean {
    if (routing === null) return false
    if (routing.vision_destination === 'cloud' && !cloudVisionConsent) return false
    if (routing.chat_destination === 'cloud' && !cloudDerivedChatConsent) return false
    return true
  }

  async function ensureBackendSession() {
    const activeController = controller
    if (
      backendSessionStarting
      || backendSession !== null
      || routing === null
      || activeController === null
      || conversationId === null
      || state.captureState !== 'active'
      || state.actualSurface === null
      || !consentSatisfied()
    ) return
    backendSessionStarting = true
    try {
      const session = await startScreenSession({
        routing,
        generation: state.generation,
        characterId,
        conversationId,
        requestedSurface: state.requestedSurface,
        actualSurface: state.actualSurface,
        cloudVisionConsent,
        cloudDerivedChatConsent,
      })
      if (
        !mounted
        || controller !== activeController
        || activeController.snapshot().generation !== session.generation
        || activeController.snapshot().captureState !== 'active'
        || characterId !== session.character_id
        || conversationId !== session.conversation_id
      ) {
        await revokeScreenSession(session, 'conversation_change').catch(() => undefined)
        return
      }
      backendSession = session
      stopHeartbeat()
      heartbeatTimer = window.setInterval(() => {
        const current = backendSession
        if (current === null) return
        void heartbeatScreenSession(current).catch(() => {
          controller?.failRecognition('backend_unavailable')
          stopAndRevoke('backend_disconnect')
        })
      }, session.heartbeat_interval_ms)
    } catch {
      controller?.failRecognition('backend_unavailable')
    } finally {
      backendSessionStarting = false
    }
  }

  function updateConsent() {
    if (backendSession !== null) stopAndRevoke('consent_revoked')
    else void ensureBackendSession()
  }

  export function clientSessionId(): string | null {
    return routing?.client_session_id ?? null
  }

  export function referenceAvailable(): boolean {
    return referenceAvailableValue
  }

  export async function captureAuthorizedRequest(
    request: SnapshotRequested,
  ): Promise<ScreenUploadResult> {
    const session = backendSession
    if (
      controller === null
      || session === null
      || request.screen_session_id !== session.screen_session_id
      || request.generation !== session.generation
    ) {
      const chat = await reportScreenCaptureFailure(
        request,
        session === null ? 'session_revoked' : 'generation_mismatch',
        characterId,
      )
      return { accepted: null, chat }
    }
    let snapshot: CapturedScreenSnapshot
    try {
      snapshot = await controller.captureSnapshot({
        request,
        clientSessionId: session.client_session_id,
      })
    } catch (error) {
      const reason = screenCaptureFailureReason(error)
      controller.failRecognition(reason)
      const chat = await reportScreenCaptureFailure(request, reason, characterId)
      return { accepted: null, chat }
    }
    controller.markRecognition('recognizing')
    try {
      const result = await uploadScreenSnapshot(snapshot, characterId)
      controller.markRecognition('succeeded', snapshot.metadata.captured_at)
      return result
    } catch (error) {
      controller.failRecognition('backend_unavailable')
      throw error
    }
  }
</script>

<section class="screen-capture" aria-label="画面参照">
  <div class="screen-summary">
    <div class="screen-summary-copy">
      <strong>画面共有</strong>
      <span title={currentTarget}>対象: {currentTarget}</span>
    </div>
    <button
      bind:this={operationButton}
      type="button"
      disabled={!sharing && (disabled || conversationId === null)}
      aria-pressed={sharing}
      aria-describedby="screen-capture-description"
      on:click={() => { if (sharing) stopSharing(); else void selectTarget() }}
    >{sharing ? '画面共有を停止' : '画面共有を開始'}</button>
  </div>
  <details class="screen-details">
    <summary>画面共有の詳細</summary>
    <p id="screen-capture-description" class="screen-description">
    {#if state.captureState === 'active'}
      共有中は、会話に必要と判断したときだけ選択した画面を参照します。共有ONだけでは画像の送信や定期的な解析を行いません。
    {:else}
      画面について会話するには、最初に共有を開始して対象を選んでください。音声だけで画面共有が自動開始することはありません。
    {/if}
    </p>
    <div class="screen-actions">
    <label>
      共有する種類
      <select bind:value={requestedSurface} disabled={disabled || busy} aria-label="共有する画面の種類">
        <option value="monitor">モニター</option>
        <option value="window">ウィンドウ</option>
        <option value="browser">ブラウザタブ</option>
      </select>
    </label>
    {#if sharing}
      <button type="button" disabled={disabled || busy} on:click={() => { void changeTarget() }}>共有対象を変更</button>
    {/if}
    <button
      type="button"
      class="reference"
      disabled={!snapshotAvailable}
      aria-describedby="explicit-reference-help"
      on:click={() => { void captureForCurrentTurn() }}
    >現在の画面を参照</button>
  </div>
  <p id="explicit-reference-help" class="screen-help">明示的に今の画面を確認させたい場合の補助操作です。通常の質問ごとに押す必要はありません。</p>

  <div class="screen-status" aria-live="polite" aria-atomic="true">
    <span>共有: {captureStatus}</span>
    <span>認識: {recognitionStatus}</span>
    {#if state.trackMuted}<span>映像待機中</span>{/if}
    {#if state.lastRecognizedCaptureAt !== null}<span>最終取得: {state.lastRecognizedCaptureAt}</span>{/if}
  </div>
  {#if state.targetLabel !== null}
    <p class="target-name">対象（この画面だけに表示）: {state.targetLabel}</p>
  {/if}
  {#if state.captureState === 'active' && state.actualSurface === 'monitor'}
    <p class="surface-guidance">
      特定のアプリについて尋ねる場合はウィンドウ共有が適しています。digital-soulsを前面にすると、モニター上の質問対象が隠れることがあります。
    </p>
  {:else if state.captureState === 'active' && state.actualSurface === 'window'}
    <p class="surface-guidance">
      選択したウィンドウが参照対象です。digital-soulsの画面とは別の対象として扱います。
    </p>
  {:else if state.captureState === 'active' && state.actualSurface === 'browser'}
    <p class="surface-guidance">
      選択したブラウザタブだけが参照対象です。共有中のタブを閉じると画面共有も終了します。
    </p>
  {/if}
  {#if routing?.vision_destination === 'cloud' || routing?.chat_destination === 'cloud'}
    <fieldset class="cloud-consent">
      <legend>クラウド送信の確認</legend>
      {#if routing?.vision_destination === 'cloud'}
        <label>
          <input
            type="checkbox"
            bind:checked={cloudVisionConsent}
            disabled={disabled}
            on:change={updateConsent}
          />
          今回の共有画像をクラウドの画面認識へ送信する
        </label>
      {/if}
      {#if routing?.chat_destination === 'cloud'}
        <label>
          <input
            type="checkbox"
            bind:checked={cloudDerivedChatConsent}
            disabled={disabled}
            on:change={updateConsent}
          />
          画面の観測文と、この共有に由来する会話内の回答をクラウドの参照判断・会話へ送信する
        </label>
      {/if}
      <p>同意は現在の共有対象・会話・送信先だけに有効です。いずれかが変わると再確認します。</p>
    </fieldset>
  {/if}
  {#if state.reasonCode !== null}
    <p class="screen-error" role="alert">{reasonLabels[state.reasonCode]}</p>
  {:else if state.captureState === 'active' && routing === null}
    <p class="integration-note">画面参照のサーバーへ接続できていません。共有画像は送信されません。</p>
  {/if}
  <video bind:this={preview} class:visible={state.captureState === 'active'} autoplay muted playsinline aria-label="共有画面のローカルプレビュー"></video>
  </details>
</section>

<style>
  .screen-capture {
    padding: 10px;
    border-top: 1px solid rgba(255, 255, 255, 0.09);
    color: #d9d1df;
    background: #17131e;
    font-size: 0.82rem;
  }
  .screen-summary { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .screen-summary-copy { min-width: 0; display: grid; gap: 2px; }
  .screen-summary-copy strong { color: #eee8f3; font-size: 0.78rem; }
  .screen-summary-copy span { overflow: hidden; color: #a9a1b5; font-size: 0.68rem; text-overflow: ellipsis; white-space: nowrap; }
  .screen-summary button { flex: 0 0 auto; min-height: 36px; padding-inline: 9px; font-size: 0.7rem; }
  .screen-details { margin-top: 8px; }
  .screen-details summary { min-height: 36px; display: flex; align-items: center; color: #bdb3c7; cursor: pointer; font-weight: 700; }
  .screen-details[open] summary { margin-bottom: 7px; }
  .screen-actions, .screen-status { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .screen-description { margin: 0 0 9px; color: #eee8f3; }
  label { display: flex; align-items: center; gap: 7px; }
  .cloud-consent { margin-top: 8px; }
  select, button {
    min-height: 40px;
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 8px;
    color: #eee8f3;
    background: #282230;
  }
  select { padding: 0 9px; }
  button { padding: 0 12px; font-weight: 700; cursor: pointer; }
  button.reference { border-color: rgba(240, 163, 193, 0.42); background: #69374c; }
  button:disabled, select:disabled { cursor: not-allowed; opacity: 0.52; }
  button:focus-visible, select:focus-visible { outline: 2px solid #f0a3c1; outline-offset: 2px; }
  .screen-status { margin-top: 8px; color: #bdb3c7; }
  .screen-help, .target-name, .surface-guidance, .screen-error, .integration-note { margin: 7px 0 0; overflow-wrap: anywhere; }
  .screen-help, .surface-guidance { color: #bdb3c7; }
  .screen-error { color: #ffb8b8; }
  .integration-note { color: #d7bf87; }
  .cloud-consent { display: grid; gap: 7px; margin: 10px 0 0; padding: 9px 11px; border: 1px solid rgba(240, 163, 193, 0.28); border-radius: 8px; }
  .cloud-consent legend { padding: 0 5px; color: #eee8f3; font-weight: 700; }
  .cloud-consent label { align-items: flex-start; }
  .cloud-consent input { width: 18px; height: 18px; flex: 0 0 auto; margin-top: 1px; accent-color: #d4729a; }
  .cloud-consent p { margin: 0; color: #bdb3c7; }
  video { display: none; width: 100%; max-height: 150px; margin-top: 10px; border-radius: 8px; background: #050407; object-fit: contain; }
  video.visible { display: block; }
  @media (max-width: 640px) { .screen-capture { padding: 10px 12px; } }
</style>
