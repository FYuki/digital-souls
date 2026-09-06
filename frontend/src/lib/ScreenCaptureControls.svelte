<script lang="ts">
  import { onDestroy, onMount } from 'svelte'

  import type { ActualSurface, RecognitionState } from './screen-perception/generated'
  import {
    BrowserScreenCaptureController,
    screenCaptureFailureReason,
    type AuthorizedSnapshotRequest,
    type CapturedScreenSnapshot,
    type ScreenCaptureState,
  } from './screen-perception/capture'

  export let characterId: string
  export let conversationId: string | null
  export let disabled = false
  export let coreConnected = false
  export let requestSnapshot: (() => Promise<AuthorizedSnapshotRequest | null>) | null = null
  export let onSnapshotCaptured: ((snapshot: CapturedScreenSnapshot) => Promise<void>) | null = null
  /** #216の会話runtimeが参照要否を判定している間だけtrueにする。 */
  export let referenceDecisionActive = false
  /** #216のsession・送信先・同意が揃い、会話から参照できる間だけtrueにする。 */
  export let contextualReferenceAvailable = false
  /** nullはローカル送信または送信先未確定、booleanはクラウド同意UIを表示する。 */
  export let cloudImageConsent: boolean | null = null
  export let cloudDerivedChatConsent: boolean | null = null
  export let onCloudImageConsentChanged: ((consented: boolean) => void) | null = null
  export let onCloudDerivedChatConsentChanged: ((consented: boolean) => void) | null = null

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
  const reasonLabels = {
    api_unavailable: 'このブラウザでは画面共有APIを利用できません。',
    insecure_context: '安全な接続ではないため画面共有を利用できません。',
    transient_activation_required: 'ボタンを押して、もう一度対象を選択してください。',
    capture_not_allowed: '画面共有が許可されませんでした。ブラウザまたは管理設定と選択内容を確認してください。',
    no_capture_source: '共有できる画面が見つかりませんでした。',
    capture_os_error: '画面共有を開始できませんでした。',
    picker_cancelled: '画面の選択を取り消しました。',
    surface_mismatch: '希望と異なる種類が選ばれたため共有しませんでした。',
    browser_surface_rejected: 'ブラウザタブは共有対象にできません。',
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
  $: recognitionStatus = referenceDecisionActive
    ? '参照が必要か確認中'
    : requestingAuthorization
      ? recognitionLabels.snapshot_requested
      : state.recognitionState === 'idle' && contextualReferenceAvailable
        ? '参照可能'
        : recognitionLabels[state.recognitionState]

  onMount(() => {
    controller = new BrowserScreenCaptureController(preview, (next) => { state = next })
    mounted = true
    const handlePageHide = () => controller?.stop('pagehide')
    window.addEventListener('pagehide', handlePageHide)
    return () => window.removeEventListener('pagehide', handlePageHide)
  })

  onDestroy(() => {
    mounted = false
    controller?.stop('pagehide')
    controller = null
  })

  $: syncCaptureContext(characterId, conversationId)
  $: syncCoreConnection(coreConnected)

  function syncCaptureContext(nextCharacterId: string, nextConversationId: string | null) {
    if (!mounted) return
    if (nextCharacterId !== previousCharacterId) {
      previousCharacterId = nextCharacterId
      previousConversationId = nextConversationId
      controller?.stop('character_change')
    } else if (nextConversationId !== previousConversationId) {
      previousConversationId = nextConversationId
      controller?.stop('conversation_change')
    }
  }

  function syncCoreConnection(nextConnected: boolean) {
    if (!mounted) return
    if (previousCoreConnected && !nextConnected) controller?.stop('backend_disconnect')
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
      controller.stop(selectedCharacterId !== characterId ? 'character_change' : 'conversation_change')
    }
    restoreFocus()
  }

  const stopSharing = () => {
    controller?.stop('user_off')
    restoreFocus()
  }

  const changeTarget = async () => {
    controller?.stop('target_change')
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

  const changeCloudImageConsent = (event: Event) => {
    onCloudImageConsentChanged?.((event.currentTarget as HTMLInputElement).checked)
  }

  const changeCloudDerivedChatConsent = (event: Event) => {
    onCloudDerivedChatConsentChanged?.((event.currentTarget as HTMLInputElement).checked)
  }
</script>

<section class="screen-capture" aria-label="画面参照">
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
      </select>
    </label>
    <button
      bind:this={operationButton}
      type="button"
      disabled={!sharing && (disabled || conversationId === null)}
      aria-pressed={sharing}
      aria-describedby="screen-capture-description"
      on:click={() => { if (sharing) stopSharing(); else void selectTarget() }}
    >{sharing ? '画面共有を停止' : '画面共有を開始'}</button>
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
  {/if}
  {#if cloudImageConsent !== null || cloudDerivedChatConsent !== null}
    <fieldset class="cloud-consent">
      <legend>クラウド送信の確認</legend>
      {#if cloudImageConsent !== null}
        <label>
          <input
            type="checkbox"
            checked={cloudImageConsent}
            disabled={disabled || onCloudImageConsentChanged === null}
            on:change={changeCloudImageConsent}
          />
          今回の共有画像をクラウドの画面認識へ送信する
        </label>
      {/if}
      {#if cloudDerivedChatConsent !== null}
        <label>
          <input
            type="checkbox"
            checked={cloudDerivedChatConsent}
            disabled={disabled || onCloudDerivedChatConsentChanged === null}
            on:change={changeCloudDerivedChatConsent}
          />
          画面の観測文と、この共有に由来する会話内の回答をクラウドの参照判断・会話へ送信する
        </label>
      {/if}
      <p>同意は現在の共有対象・会話・送信先だけに有効です。いずれかが変わると再確認します。</p>
    </fieldset>
  {/if}
  {#if state.reasonCode !== null}
    <p class="screen-error" role="alert">{reasonLabels[state.reasonCode]}</p>
  {:else if state.captureState === 'active' && requestSnapshot === null}
    <p class="integration-note">画面参照のサーバー連携は準備中です。共有画像は送信されません。</p>
  {/if}
  <video bind:this={preview} class:visible={state.captureState === 'active'} autoplay muted playsinline aria-label="共有画面のローカルプレビュー"></video>
</section>

<style>
  .screen-capture {
    padding: 10px 24px;
    border-top: 1px solid rgba(255, 255, 255, 0.09);
    color: #d9d1df;
    background: #17131e;
    font-size: 0.82rem;
  }
  .screen-actions, .screen-status { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; }
  .screen-description { margin: 0 0 9px; color: #eee8f3; }
  label { display: flex; align-items: center; gap: 7px; }
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
  video { display: none; width: min(100%, 560px); max-height: 180px; margin-top: 10px; border-radius: 8px; background: #050407; object-fit: contain; }
  video.visible { display: block; }
  @media (max-width: 640px) { .screen-capture { padding: 10px 12px; } }
</style>
