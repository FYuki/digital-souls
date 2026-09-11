<script lang="ts">
  import ToolUseStatus from './lib/ToolUseStatus.svelte'
  import { onMount, tick } from 'svelte'

  import AudioRecorder from './lib/AudioRecorder.svelte'
  import type { SpeechActivity } from './lib/AudioRecorder.svelte'
  import CharacterPortrait from './lib/CharacterPortrait.svelte'
  import ChatWindow from './lib/ChatWindow.svelte'
  import type {SettledVoiceTurnDisplay} from './lib/voice-turn-display'
  import type {SelectedConversationContext} from './lib/conversations/controller'
  import ConversationSidebar from './lib/ConversationSidebar.svelte'
  import InputBar from './lib/InputBar.svelte'
  import MemoryManagement from './lib/MemoryManagement.svelte'
  import AddonManagement from './lib/AddonManagement.svelte'
  import type { ApprovalRequest } from './lib/addon-admin/approvals'
  import { createAddonController, aggregateBadge } from './lib/addon-admin/controller'
  import ScreenCaptureControls from './lib/ScreenCaptureControls.svelte'
  import type { ScreenUploadResult } from './lib/screen-perception/client'
  import { listCharacters, rescanCharacters } from './lib/characters/client'
  import { sendChatRequest, parseChatResponseBody } from './lib/chat/client'
  import { createConversationSessionManager } from './lib/conversation-session'
  import {
    archiveConversation,
    createConversation,
    hardDeleteConversation,
    listActiveConversations,
    listArchivedConversations,
    listConversationTurns,
    renameConversation,
    unarchiveConversation,
  } from './lib/conversations/client'
  import {
    createConversationController,
  } from './lib/conversations/controller'
  import { createSidebarController } from './lib/sidebar/controller'
  import {
    getUiSettings,
    setCharacterPinned,
    setCharacterVisibility,
    setThreadPinned,
    updateUiPreferences,
  } from './lib/ui-settings/client'
  import type { VoiceSessionEvent } from './lib/voice-session/generated'
  import {
    LiveKitVoiceSessionController,
    type VoiceSessionSnapshot,
    type VoiceSessionContext,
  } from './livekit/voice-session'
  import {VoiceHistoryProjection} from './livekit/history-projection'

  const INITIAL_CHARACTER_ID = 'miori'
  const ERROR_MESSAGE = '応答の取得に失敗しました。'
  const conversationController = createConversationController(
    INITIAL_CHARACTER_ID,
    ERROR_MESSAGE,
    {
      listActive: listActiveConversations,
      listArchived: listArchivedConversations,
      listTurns: listConversationTurns,
      create: createConversation,
      archive: archiveConversation,
      unarchive: unarchiveConversation,
      hardDelete: hardDeleteConversation,
    },
    createConversationSessionManager(),
  )
  const sidebarController = createSidebarController({
    listCatalog: listCharacters,
    rescanCatalog: rescanCharacters,
    getSettings: getUiSettings,
    updatePreferences: updateUiPreferences,
    setCharacterVisibility,
    setCharacterPinned,
    setThreadPinned,
    listActive: listActiveConversations,
    listArchived: listArchivedConversations,
    create: createConversation,
    rename: renameConversation,
    archive: archiveConversation,
    unarchive: unarchiveConversation,
    hardDelete: hardDeleteConversation,
  }, ERROR_MESSAGE)
  type PendingRequest = 'text' | null

  let pendingRequest: PendingRequest = null
  let screenControls: ScreenCaptureControls | null = null
  let screenReferenceAvailable = false
  let screenReferenceDecisionActive = false
  let applicationError: string | null = null
  const addonController = createAddonController()
  let showingAddonManagement = false
  let addonReturnFocus: HTMLButtonElement | null = null
  async function closeAddonManagement() {
    showingAddonManagement = false
    sidebarOpen = true
    await tick()
    addonReturnFocus?.focus()
  }
  let showingMemoryManagement = false
  let activeUtteranceId: string | null = null
  let endingVoiceSession = false
  let voiceSourceLabel = ''
  let voiceSwitchMutedSessionId: string | null = null
  let voiceErrors: Record<string, string> = {}
  let sidebarOpen = true
  let compactLayout = false
  let visualViewportHeight: number | null = null
  let visualViewportOffsetTop = 0
  type LiveVoiceTurn = {
    context: SelectedConversationContext
    historyTurnId?: string
    responseId: string | null
    sourceUtteranceIds: string[]
    userContent: string
    assistantContent: string
    lastTextSequence: number
  }
  let liveVoiceTurn: LiveVoiceTurn | null = null
  let settledVoiceTurns: (SettledVoiceTurnDisplay & {context: SelectedConversationContext})[] = []
  type FailedVoiceTurn = {
    responseId: string
    characterId: string
    conversationId: string
    userContent: string
    assistantContent: string
  }
  let failedVoiceTurns: FailedVoiceTurn[] = []
  $: visibleFailedVoiceTurns = failedVoiceTurns.filter((turn) => (
    turn.characterId === $conversationController.character
    && turn.conversationId === $conversationController.selectedConversationId
  ))
  const finalizedUtterances = new Map<string, string>()
  const voiceHistory = new VoiceHistoryProjection()
  let voiceSnapshot: VoiceSessionSnapshot = {
    phase: 'idle',
    input: 'inactive',
    response: 'idle',
    playback: 'idle',
    context: null,
    sessionId: null,
    activeResponseId: null,
    textSubmissions: [],
  }
  const voiceSession = new LiveKitVoiceSessionController(
    (snapshot) => {
      if (snapshot.phase === 'reconnecting') activeUtteranceId = null
      voiceSnapshot = snapshot
    },
    receiveVoiceCoreEvent,
  )

  function receiveVoiceCoreEvent(event: VoiceSessionEvent, voiceContext: VoiceSessionContext) {
    const selected = conversationController.selectedContext()
    const context: SelectedConversationContext = selected?.character === voiceContext.characterId
      && selected.conversationId === voiceContext.conversationId ? selected
      : {character: voiceContext.characterId, conversationId: voiceContext.conversationId, version: -1}
    const projected = voiceHistory.receive(event, voiceContext, voiceSnapshot.textSubmissions)
    if (event.type === 'utterance_finalized' && event.utterance_id !== undefined) {
      const transcript = event.transcript ?? ''
      if (event.should_response === false) return
      if (screenReferenceAvailable) screenReferenceDecisionActive = true
      finalizedUtterances.set(event.utterance_id, transcript)
      if (liveVoiceTurn === null) {
        liveVoiceTurn = {
          context,
          responseId: null,
          sourceUtteranceIds: [event.utterance_id],
          userContent: transcript,
          assistantContent: '',
          lastTextSequence: 0,
        }
      } else if (liveVoiceTurn.responseId === null) {
        liveVoiceTurn = {
          ...liveVoiceTurn,
          sourceUtteranceIds: [...liveVoiceTurn.sourceUtteranceIds, event.utterance_id],
          userContent: [liveVoiceTurn.userContent, transcript]
            .filter((text) => text !== '')
            .join('\n'),
        }
      }
      return
    }
    if (event.type === 'response_started' && event.response_id !== undefined) {
      if (projected === null) return
      const sourceIds = event.source_utterance_ids ?? []
      liveVoiceTurn = {
        context,
        ...(event.history_turn_id === undefined ? {} : {historyTurnId: event.history_turn_id}),
        responseId: event.response_id,
        sourceUtteranceIds: sourceIds,
        userContent: projected.userContent,
        assistantContent: '',
        lastTextSequence: 0,
      }
      return
    }
    if (
      event.type === 'response_delta'
      && event.response_id !== undefined
      && event.text_sequence !== undefined
      && event.text !== undefined
      && liveVoiceTurn?.responseId === event.response_id
      && projected !== null
    ) {
      screenReferenceDecisionActive = false
      liveVoiceTurn = {
        ...liveVoiceTurn,
        assistantContent: projected.assistantContent,
        lastTextSequence: projected.lastTextSequence,
      }
      return
    }
    if (
      ['response_completed', 'response_cancelled', 'response_failed'].includes(event.type)
      && liveVoiceTurn !== null
      && event.response_id === liveVoiceTurn.responseId
    ) {
      screenReferenceDecisionActive = false
      const responseContext = liveVoiceTurn.context
      if (event.type === 'response_failed') {
        const context = responseContext
        if (context !== null) {
          failedVoiceTurns = [...failedVoiceTurns, {
            responseId: event.response_id,
            characterId: context.character,
            conversationId: context.conversationId,
            userContent: liveVoiceTurn.userContent,
            assistantContent: liveVoiceTurn.assistantContent,
          }]
        }
      }
      for (const utteranceId of liveVoiceTurn.sourceUtteranceIds) {
        finalizedUtterances.delete(utteranceId)
      }
      if (event.type !== 'response_failed' && liveVoiceTurn.historyTurnId !== undefined
        && liveVoiceTurn.responseId !== null) {
        settledVoiceTurns = [...settledVoiceTurns, {...liveVoiceTurn,
          historyTurnId: liveVoiceTurn.historyTurnId, responseId: liveVoiceTurn.responseId,
          terminal: event.type === 'response_cancelled' ? 'cancelled' : 'completed'}]
      }
      liveVoiceTurn = null
      if (event.type !== 'response_failed') {
        void conversationController.refreshTurns(responseContext).then(() => {
          // 失敗時や別会話の再取得では、未反映の表示を消さない。
          const current = conversationController.selectedContext()
          if (current?.character !== responseContext.character || current.conversationId !== responseContext.conversationId
            || current.version !== responseContext.version) return
          const loadedIds = new Set($conversationController.turns.map(turn => turn.turn_id))
          settledVoiceTurns = settledVoiceTurns.filter(turn => !loadedIds.has(turn.historyTurnId))
        })
        void sidebarController.refreshCharacter(responseContext.character)
      }
      return
    }
    if (event.type === 'error') {
      screenReferenceDecisionActive = false
      if (event.utterance_id !== undefined) finalizedUtterances.delete(event.utterance_id)
      voiceErrors = {...voiceErrors, [`${context.character}:${context.conversationId}`]: ERROR_MESSAGE}
      return
    }
    if (event.type === 'utterance_discarded' && event.utterance_id !== undefined) {
      screenReferenceDecisionActive = false
      finalizedUtterances.delete(event.utterance_id)
      if (liveVoiceTurn?.responseId === null) liveVoiceTurn = null
    }
  }

  $: interactionsDisabled = pendingRequest !== null
    || $conversationController.pending
    || $conversationController.deleteCandidate !== null
  $: selectedVoiceContext = $conversationController.selectedConversationId === null ? null : {
    characterId: $conversationController.character, conversationId: $conversationController.selectedConversationId,
  }
  $: voiceMatchesSelection = voiceSnapshot.context !== null && selectedVoiceContext !== null
    && voiceSnapshot.context.characterId === selectedVoiceContext.characterId
    && voiceSnapshot.context.conversationId === selectedVoiceContext.conversationId
  $: voiceMismatch = voiceSnapshot.sessionId !== null && !voiceMatchesSelection
  $: selectedTextSubmission = [...voiceSnapshot.textSubmissions].reverse().find(entry =>
    entry.context.characterId === $conversationController.character
    && entry.context.conversationId === $conversationController.selectedConversationId
    && (entry.sessionId === voiceSnapshot.sessionId || entry.status === 'sending' || entry.status === 'confirming')) ?? null
  $: visibleLiveVoiceTurn = liveVoiceTurn?.context.character === $conversationController.character
    && liveVoiceTurn.context.conversationId === $conversationController.selectedConversationId ? liveVoiceTurn : null
  $: visibleSettledVoiceTurns = settledVoiceTurns.filter(turn => turn.context.character === $conversationController.character
    && turn.context.conversationId === $conversationController.selectedConversationId)
  $: visibleVoiceError = voiceErrors[`${$conversationController.character}:${$conversationController.selectedConversationId}`] ?? ''
  $: syncVoiceSelection(
    $conversationController.character,
    $conversationController.selectedConversationId,
    voiceSnapshot.sessionId,
  )
  $: voiceRecorderDisabled = $conversationController.selectedConversationId === null
    || voiceMismatch
    || $conversationController.pending
    || $conversationController.deleteCandidate !== null
    || endingVoiceSession
    || voiceSnapshot.phase === 'connecting'
  $: voiceRecorderForceOff = $conversationController.selectedConversationId === null
    || voiceMismatch
    || endingVoiceSession
    || voiceSnapshot.phase === 'error'
    || voiceSnapshot.phase === 'ended'
  $: sessionStatus = ({
    idle: '停止',
    connecting: '接続中',
    listening: '接続済み',
    muted: '接続済み',
    reconnecting: '再接続中',
    ended: '終了',
    error: 'エラー',
  } as const)[voiceSnapshot.phase]
  $: inputStatus = ({
    inactive: '停止',
    muted: 'ミュート',
    suppressed: 'テキスト入力中',
    listening: '聞き取り中',
    transcribing: '文字起こし中',
  } as const)[voiceSnapshot.input]
  $: responseStatus = ({
    idle: '待機',
    thinking: '考え中',
    generating: '応答生成中',
    interrupting: '割り込み処理中',
  } as const)[voiceSnapshot.response]
  $: playbackStatus = ({
    idle: '待機',
    playing: '再生中',
    stopped: '停止済み',
  } as const)[voiceSnapshot.playback]

  const appendApplicationError = () => {
    applicationError = ERROR_MESSAGE
  }

  function syncVoiceSelection(character: string, conversationId: string | null, sessionId: string | null) {
    const active = voiceSnapshot.context
    if (active === null || endingVoiceSession) return
    if (active.characterId === character && active.conversationId === conversationId) return
    // 表示選択ではsessionと生成を終えない。device停止とBEのmute状態をそろえる。
    activeUtteranceId = null
    if (sessionId !== null && voiceSwitchMutedSessionId !== sessionId) {
      voiceSwitchMutedSessionId = sessionId
      void voiceSession.muteForThreadSwitch().catch(appendApplicationError)
    }
  }

  onMount(() => {
    addonController.start()
    const refreshAddons = () => { void addonController.refresh() }
    window.addEventListener('focus', refreshAddons)
    const compactQuery = window.matchMedia?.('(max-width: 900px)')
    const viewport = window.visualViewport
    const updateLayout = () => {
      const wasCompact = compactLayout
      compactLayout = compactQuery?.matches ?? false
      if (compactLayout && !wasCompact) sidebarOpen = false
      if (!compactLayout && wasCompact) sidebarOpen = true
    }
    compactLayout = compactQuery?.matches ?? false
    sidebarOpen = !compactLayout
    const updateViewport = () => {
      visualViewportHeight = viewport?.height ?? window.innerHeight
      visualViewportOffsetTop = viewport?.offsetTop ?? 0
    }
    updateViewport()
    compactQuery?.addEventListener('change', updateLayout)
    viewport?.addEventListener('resize', updateViewport)
    viewport?.addEventListener('scroll', updateViewport)
    window.addEventListener('resize', updateViewport)
    void sidebarController.initialize()
    return () => {
      addonController.destroy()
      window.removeEventListener('focus', refreshAddons)
      compactQuery?.removeEventListener('change', updateLayout)
      viewport?.removeEventListener('resize', updateViewport)
      viewport?.removeEventListener('scroll', updateViewport)
      window.removeEventListener('resize', updateViewport)
      void voiceSession.end().catch(() => undefined)
    }
  })

  const handleSend = async (message: string, screenReference = false): Promise<'accepted' | 'failed' | {inputId: string}> => {
    const text = message.trim()
    const context = conversationController.selectedContext()
    if (text.length === 0 || interactionsDisabled || context === null) return 'failed'
    const target = {characterId: context.character, conversationId: context.conversationId}
    if (voiceSession.matchesContext(target)) {
      try {
        voiceErrors = {...voiceErrors, [`${context.character}:${context.conversationId}`]: ''}
        const inputId = await voiceSession.submitText(target, text)
        return {inputId}
      } catch {
        conversationController.reportConversationError(context)
        return 'failed'
      }
    }
    pendingRequest = 'text'
    if (screenReferenceAvailable) screenReferenceDecisionActive = true
    applicationError = null
    try {
      const response = await sendChatRequest({
        character: context.character,
        conversationId: context.conversationId,
        message: text,
        screenReference,
        screenClientSessionId: screenControls?.clientSessionId() ?? null,
      })
      let completed = response.kind === 'completed' ? response.chat : null
      if (response.kind === 'snapshot_requested') {
        screenReferenceDecisionActive = false
        if (screenControls === null) throw new Error('screen capture is not active')
        const upload: ScreenUploadResult = await screenControls.captureAuthorizedRequest(
          response.request,
        )
        completed = upload.chat
      }
      if (completed === null) throw new Error('text screen request did not return a chat turn')
      if (conversationController.selectedContext()?.version !== context.version) return 'accepted'
      conversationController.appendTurn(context, completed.turn)
      void sidebarController.refreshCharacter(context.character)
      return 'accepted'
    } catch {
      conversationController.reportConversationError(context)
      return 'failed'
    } finally {
      screenReferenceDecisionActive = false
      if (conversationController.selectedContext()?.version === context.version) pendingRequest = null
    }
  }

  const handleActionContinue = async (requestId: string): Promise<void | 'ended'> => {
    const context = conversationController.selectedContext()
    if (context === null) return
    const voiceId = voiceSnapshot.context?.characterId === context.character
      && voiceSnapshot.context.conversationId === context.conversationId
      ? voiceSnapshot.sessionId : null
    const response = await fetch(`/api/addon-actions/requests/${encodeURIComponent(requestId)}/continue`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ character: context.character, conversation_id: context.conversationId,
        ...(voiceId ? { voice_session_id: voiceId } : {}),
      }),
    })
    if (!response.ok) throw new Error('action continuation failed')
    const body = await response.json()
    if (body.state === 'ended') return 'ended'
    if (body.state === 'voice_started' || body.state === 'continuing') return
    const chat = parseChatResponseBody(body, context.character)
    if (conversationController.selectedContext()?.version !== context.version) return
    conversationController.appendTurn(context, chat.turn)
    void sidebarController.refreshCharacter(context.character)
  }

  function handleAdminContinued(item: ApprovalRequest, body: Record<string, unknown>) {
    if (typeof body.state === 'string') return
    const chat = parseChatResponseBody(body, item.character_id)
    const context = conversationController.selectedContext()
    if (context?.character === item.character_id && context.conversationId === item.session_id) {
      conversationController.appendTurn(context, chat.turn)
    }
    void sidebarController.refreshCharacter(item.character_id)
  }

  const handleSelectConversation = async (character: string, conversationId: string) => {
    if (interactionsDisabled) return
    showingMemoryManagement = false
    showingAddonManagement = false
    if (character !== $conversationController.character) {
      await conversationController.loadCharacter(character)
    }
    await conversationController.selectConversation(conversationId)
    if (compactLayout) sidebarOpen = false
  }

  const handleCreatedConversation = async (character: string, conversation: { conversation_id: string }) => {
    await handleSelectConversation(character, conversation.conversation_id)
  }

  const handleRemovedConversation = (character: string, conversationId: string) => {
    conversationController.clearSelection(character, conversationId)
  }

  $: currentCharacterEntry = $sidebarController.catalog.find(
    (item) => item.character_id === $conversationController.character,
  )
  $: currentCharacterState = $sidebarController.settings?.characters.find(
    (item) => item.character_id === $conversationController.character,
  )
  $: currentConversation = [
    ...($sidebarController.activeByCharacter[$conversationController.character] ?? []),
    ...($sidebarController.archivedByCharacter[$conversationController.character] ?? []),
  ].find((item) => item.conversation_id === $conversationController.selectedConversationId)
  $: portraitLayout = compactLayout
    ? 'background'
    : ($sidebarController.settings?.desktop_portrait_layout ?? 'right')
  $: historyHeightPercent = compactLayout
    ? ($sidebarController.settings?.compact_history_height_percent ?? 75)
    : ($sidebarController.settings?.desktop_history_height_percent ?? 75)

  const ensureVoiceSession = async () => {
    const context = conversationController.selectedContext()
    if (context === null) throw new Error('Conversation is not selected')
    voiceSourceLabel = currentConversation?.title ?? '音声会話のスレッド'
    applicationError = null
    try {
      voiceSession.setScreenIntegration(
        screenControls?.clientSessionId() ?? null,
        (event) => {
          const controls = screenControls
          if (controls === null) return
          screenReferenceDecisionActive = false
          void controls.captureAuthorizedRequest(event).catch(appendApplicationError)
        },
      )
      await voiceSession.ensureSession({
        characterId: context.character,
        conversationId: context.conversationId,
      })
    } catch (error) {
      appendApplicationError()
      throw error
    }
  }

  const prepareVoiceMicrophone = async () => {
    await ensureVoiceSession()
    // getUserMediaの失敗も利用者による開始・再試行として数える。
    voiceSession.recordMicrophoneActivationAttempt()
  }

  const resumeVoiceMicrophone = async (stream: MediaStream) => {
    try {
      const context = conversationController.selectedContext()
      if (context === null || !voiceSession.matchesContext({characterId: context.character, conversationId: context.conversationId})) return
      voiceSwitchMutedSessionId = null
      await voiceSession.resumeMicrophone(stream)
    } catch (error) {
      appendApplicationError()
      throw error
    }
  }

  const muteVoiceMicrophone = async () => {
    try {
      activeUtteranceId = null
      await voiceSession.muteMicrophone()
    } catch (error) {
      appendApplicationError()
      throw error
    }
  }

  const handleSpeechStarted = ({ clientMs }: SpeechActivity) => {
    if (voiceSnapshot.phase === 'reconnecting') return
    const utteranceId = crypto.randomUUID()
    activeUtteranceId = utteranceId
    void voiceSession.speechStarted(utteranceId, clientMs).catch(appendApplicationError)
  }

  const handleSpeechStopped = ({ clientMs }: SpeechActivity) => {
    const utteranceId = activeUtteranceId
    activeUtteranceId = null
    if (utteranceId === null) return
    void voiceSession.speechStopped(utteranceId, clientMs).catch(appendApplicationError)
  }

  const endVoiceSession = async () => {
    if (endingVoiceSession) return
    endingVoiceSession = true
    activeUtteranceId = null
    try {
      await voiceSession.end()
    } catch {
      appendApplicationError()
    } finally {
      endingVoiceSession = false
    }
  }

  const restartVoiceSession = async () => {
    voiceSession.recordRetryAttempt()
    try {
      await ensureVoiceSession()
    } catch {
      // ensureVoiceSessionが利用者向けerrorを設定する。
    }
  }
</script>

<main
  class="app-shell"
  style={`--visual-viewport-height: ${visualViewportHeight === null ? '100dvh' : `${visualViewportHeight}px`}; --visual-viewport-top: ${visualViewportOffsetTop}px`}
>
  {#if sidebarOpen && compactLayout}
      <button class="drawer-backdrop" type="button" aria-label="サイドバーを閉じる" on:click={() => { sidebarOpen = false }}></button>
  {/if}
  <ConversationSidebar
    open={sidebarOpen}
    state={$sidebarController}
    controller={sidebarController}
    selectedCharacter={$conversationController.character}
    selectedConversationId={$conversationController.selectedConversationId}
    disabled={interactionsDisabled}
    onClose={() => { sidebarOpen = false }}
    onSelect={(character, conversationId) => { void handleSelectConversation(character, conversationId) }}
    onCreated={(character, conversation) => { void handleCreatedConversation(character, conversation) }}
    onRemoved={handleRemovedConversation}
    onRenamed={() => undefined}
    addonBadge={aggregateBadge($addonController.items)}
    onOpenAddons={(trigger) => { addonReturnFocus = trigger; showingMemoryManagement = false; showingAddonManagement = true; if (compactLayout) sidebarOpen = false }}
    onOpenMemory={() => { showingAddonManagement = false; showingMemoryManagement = true; if (compactLayout) sidebarOpen = false }}
  >
    <ScreenCaptureControls
      slot="screen-controls"
      bind:this={screenControls}
      characterId={$conversationController.character}
      conversationId={$conversationController.selectedConversationId}
      disabled={interactionsDisabled}
      referenceDecisionActive={screenReferenceDecisionActive}
      onReferenceAvailabilityChanged={(available) => { screenReferenceAvailable = available }}
    />
  </ConversationSidebar>
  {#if !sidebarOpen}
    <button class="floating-menu" type="button" aria-label="サイドバーを開く" on:click={() => { sidebarOpen = true }}>☰</button>
  {/if}
  {#if showingAddonManagement}
    <section class="content-panel memory-panel">
      <AddonManagement controller={addonController} onContinued={handleAdminContinued} onClose={() => { void closeAddonManagement() }} />
    </section>
  {:else if showingMemoryManagement}
    <section class="content-panel memory-panel">
      <MemoryManagement character={$conversationController.character} onClose={() => { showingMemoryManagement = false }} />
    </section>
  {/if}
  {#if !showingMemoryManagement}
  <section class="chat-panel" class:management-hidden={showingAddonManagement} aria-hidden={showingAddonManagement} aria-label={`${currentCharacterEntry?.display_name ?? $conversationController.character}とのチャット`}>
    <header class="chat-header">
      <p class="eyebrow">digital-souls</p>
      <div class="current-thread">
        <h1>{currentConversation?.title ?? 'スレッド未選択'}</h1>
        <p>{currentCharacterEntry?.display_name ?? $conversationController.character}</p>
      </div>
      {#if currentCharacterState?.visible === false}
        <span class="hidden-badge">一覧から非表示中</span>
      {/if}
    </header>
    <div
      class:portrait-background={portraitLayout === 'background'}
      class:portrait-right={portraitLayout === 'right'}
      class="conversation-stage"
      data-portrait-layout={portraitLayout}
      data-history-height={historyHeightPercent}
      style={`--history-height: ${historyHeightPercent}%`}
    >
      <div class="portrait-layer">
        <CharacterPortrait character={currentCharacterEntry ?? null} />
      </div>
      <div class="history-layer">
        <ChatWindow
          turns={$conversationController.turns}
          characterName={currentCharacterEntry?.display_name ?? $conversationController.character}
          failedVoiceTurns={visibleFailedVoiceTurns}
          liveVoiceTurn={visibleLiveVoiceTurn}
          settledVoiceTurns={visibleSettledVoiceTurns}
        />
      </div>
    </div>
    {#if applicationError !== null || $conversationController.error !== null || visibleVoiceError}
      <p class="application-error" role="alert">{applicationError ?? $conversationController.error ?? visibleVoiceError}</p>
    {/if}
    {#if voiceSnapshot.phase !== 'idle'}
      <section class="voice-status" aria-label="音声会話の状態" aria-live="polite">
        {#if voiceMismatch}<span>「{voiceSourceLabel}」の音声会話を継続中</span>{/if}
        <span>セッション: {sessionStatus}</span>
        <span>入力: {inputStatus}</span>
        <span>応答: {responseStatus}</span>
        <span>再生: {playbackStatus}</span>
        {#if voiceSnapshot.phase === 'reconnecting'}
          <strong>接続を復旧しています。会話履歴は保持されます。</strong>
        {:else if voiceSnapshot.phase === 'ended' || voiceSnapshot.phase === 'error'}
          <strong>音声会話は停止しました。テキスト履歴は保持されています。</strong>
          <button type="button" on:click={() => { void restartVoiceSession() }}>音声会話を再開</button>
        {/if}
      </section>
    {/if}
    <div class="input-area">
      {#if $conversationController.selectedConversationId !== null}
        {#key `${$conversationController.character}:${$conversationController.selectedConversationId}`}
          <ToolUseStatus
            character={$conversationController.character}
            conversationId={$conversationController.selectedConversationId}
            onStop={endVoiceSession}
            onContinue={handleActionContinue}
          />
        {/key}
      {/if}
      <InputBar
        onSend={handleSend}
        characterName={currentCharacterEntry?.display_name ?? $conversationController.character}
        disabled={interactionsDisabled || $conversationController.selectedConversationId === null}
        sendDisabled={voiceMatchesSelection && ['connecting', 'reconnecting', 'ended', 'error'].includes(voiceSnapshot.phase)}
        screenReferenceAvailable={screenReferenceAvailable && !voiceMatchesSelection}
        threadKey={`${$conversationController.character}:${$conversationController.selectedConversationId}`}
        submission={selectedTextSubmission}
        onFocusChanged={(focused) => {
          const context = conversationController.selectedContext()
          if (context !== null) void voiceSession.setTextInputFocused({
            characterId: context.character, conversationId: context.conversationId,
          }, focused).catch(appendApplicationError)
        }}
      />
      <AudioRecorder
        suspended={voiceSnapshot.phase === 'reconnecting' || voiceSnapshot.input === 'suppressed'}
        disabled={voiceRecorderDisabled}
        forceOff={voiceRecorderForceOff}
        continuous={true}
        onBeforeEnable={prepareVoiceMicrophone}
        onMicrophoneEnabled={resumeVoiceMicrophone}
        onMicrophoneDisabled={muteVoiceMicrophone}
        onSpeechStarted={handleSpeechStarted}
        onSpeechStopped={handleSpeechStopped}
        onAudioCaptured={() => undefined}
        onError={appendApplicationError}
      />
      {#if voiceSnapshot.sessionId !== null}
        <button
          type="button"
          class="end-voice-session"
          disabled={endingVoiceSession}
          on:click={() => { void endVoiceSession() }}
        >音声会話を終了</button>
      {/if}
    </div>
  </section>
  {/if}
</main>

<style>
  .chat-panel.management-hidden { display: none; }
  .app-shell {
    position: relative;
    height: var(--visual-viewport-height, 100dvh);
    display: flex;
    align-items: stretch;
    overflow: hidden;
    background:
      radial-gradient(circle at 78% 8%, rgba(156, 130, 255, 0.14), transparent 30%),
      #0c0a12;
  }

  .chat-panel, .content-panel {
    min-width: 0;
    min-height: 0;
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    color: #f8f3ff;
    background: #100d17;
  }

  .chat-header {
    position: relative;
    z-index: 20;
    display: flex;
    min-height: 64px;
    box-sizing: border-box;
    align-items: center;
    gap: 14px;
    padding: 10px 22px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.09);
    background: rgba(12, 10, 18, 0.84);
    backdrop-filter: blur(18px);
  }

  .eyebrow {
    margin: 0;
    color: #c4b6da;
    font-size: 0.68rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  .current-thread { min-width: 0; flex: 1; }
  h1 {
    margin: 0;
    overflow: hidden;
    color: #fbf7ff;
    font-size: 0.94rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .current-thread p { margin: 3px 0 0; color: #8d8598; font-size: 0.68rem; }
  .hidden-badge { padding: 5px 8px; border: 1px solid rgba(240, 163, 193, 0.25); border-radius: 999px; color: #e5b5c9; font-size: 0.65rem; }

  .conversation-stage {
    position: relative;
    min-width: 0;
    min-height: 0;
    flex: 1;
    overflow: hidden;
    background:
      radial-gradient(circle at 72% 38%, rgba(240, 163, 193, 0.08), transparent 35%),
      #100d17;
  }

  .portrait-right {
    display: grid;
    grid-template-columns: minmax(0, 1fr) clamp(280px, 34vw, 520px);
  }

  .portrait-right .portrait-layer {
    position: relative;
    grid-column: 2;
    grid-row: 1;
    min-width: 0;
    border-left: 1px solid rgba(255, 255, 255, 0.08);
  }

  .portrait-right .history-layer {
    min-width: 0;
    min-height: 0;
    display: flex;
    grid-column: 1;
    grid-row: 1;
  }

  .portrait-background .portrait-layer {
    position: absolute;
    z-index: 0;
    inset: 0;
  }

  .portrait-background .history-layer {
    position: absolute;
    z-index: 1;
    right: 0;
    bottom: 0;
    left: 0;
    height: var(--history-height);
    display: flex;
    min-height: 0;
    background: linear-gradient(180deg, transparent, rgba(12, 10, 18, 0.18) 28%);
  }

  :global(.history-layer .messages) {
    width: 100%;
    height: 100%;
    box-sizing: border-box;
    background: transparent;
  }

  :global(.portrait-background .message) {
    border-color: rgba(255, 255, 255, 0.16);
    background: rgba(33, 27, 42, 0.9);
    backdrop-filter: blur(8px);
  }

  :global(.portrait-background .message.user) {
    background: rgba(141, 66, 96, 0.92);
  }

  .floating-menu { position: fixed; z-index: 55; top: 14px; left: 14px; display: grid; width: 44px; height: 44px; place-items: center; border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 12px; color: #f8f3ff; background: rgba(27, 23, 38, 0.9); box-shadow: 0 10px 25px rgba(0, 0, 0, 0.28); cursor: pointer; backdrop-filter: blur(12px); }
  .floating-menu:focus-visible { outline: 2px solid #f0a3c1; outline-offset: 2px; }
  .drawer-backdrop { position: fixed; z-index: 45; inset: 0; border: 0; background: rgba(3, 2, 6, 0.62); }
  .memory-panel { overflow: auto; padding: 18px; }

  .input-area {
    display: flex;
    flex-wrap: wrap;
    align-items: stretch;
    gap: 12px;
    padding: 16px 24px 20px;
    border-top: 1px solid rgba(255, 255, 255, 0.09);
    background: #15111d;
  }

  .application-error {
    margin: 0;
    padding: 8px 24px;
    color: #ffb8b8;
  }

  .voice-status {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 14px;
    padding: 10px 24px;
    border-top: 1px solid rgba(255, 255, 255, 0.09);
    color: #d4ccd9;
    background: #17131e;
    font-size: 0.88rem;
  }

  .voice-status strong {
    flex-basis: 100%;
    font-weight: 600;
  }

  .voice-status button {
    min-height: 44px;
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 8px;
    color: #eee8f3;
    background: #282230;
    font-weight: 700;
  }

  :global(.input-area .input-bar) {
    flex: 1;
    min-width: 0;
    padding: 0;
    border-top: 0;
    background: transparent;
  }

  .end-voice-session {
    flex: 0 0 auto;
    min-height: 44px;
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 8px;
    color: #eee8f3;
    background: #282230;
    font-weight: 700;
  }

  @media (max-width: 900px) {
    .app-shell { position: fixed; top: var(--visual-viewport-top, 0); right: 0; left: 0; }
    :global(.app-shell > .sidebar) { position: fixed; z-index: 50; inset: 0 auto 0 0; width: min(292px, calc(100vw - 36px)); }
    .chat-header { padding-left: 68px; }
  }

  @media (max-width: 640px) {
    .chat-panel { min-height: 0; }

    .input-area {
      padding: 12px;
      gap: 8px;
    }
  }
</style>
