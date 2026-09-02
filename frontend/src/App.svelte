<script lang="ts">
  import { onMount } from 'svelte'

  import AudioRecorder from './lib/AudioRecorder.svelte'
  import type { SpeechActivity } from './lib/AudioRecorder.svelte'
  import CharacterPortrait from './lib/CharacterPortrait.svelte'
  import ChatWindow from './lib/ChatWindow.svelte'
  import ConversationSidebar from './lib/ConversationSidebar.svelte'
  import InputBar from './lib/InputBar.svelte'
  import MemoryManagement from './lib/MemoryManagement.svelte'
  import { listCharacters, rescanCharacters } from './lib/characters/client'
  import { sendChatMessage } from './lib/chat/client'
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
  } from './livekit/voice-session'

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
  let applicationError: string | null = null
  let showingMemoryManagement = false
  let activeUtteranceId: string | null = null
  let endingVoiceSession = false
  let sidebarOpen = true
  let compactLayout = false
  let visualViewportHeight: number | null = null
  let visualViewportOffsetTop = 0
  type LiveVoiceTurn = {
    responseId: string | null
    sourceUtteranceIds: string[]
    userContent: string
    assistantContent: string
    lastTextSequence: number
  }
  let liveVoiceTurn: LiveVoiceTurn | null = null
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
  let voiceSnapshot: VoiceSessionSnapshot = {
    phase: 'idle',
    input: 'inactive',
    response: 'idle',
    playback: 'idle',
    context: null,
    sessionId: null,
    activeResponseId: null,
  }
  const voiceSession = new LiveKitVoiceSessionController(
    (snapshot) => { voiceSnapshot = snapshot },
    receiveVoiceCoreEvent,
  )

  function receiveVoiceCoreEvent(event: VoiceSessionEvent) {
    if (event.type === 'utterance_finalized' && event.utterance_id !== undefined) {
      const transcript = event.transcript ?? ''
      if (event.should_response === false) return
      finalizedUtterances.set(event.utterance_id, transcript)
      if (liveVoiceTurn === null) {
        liveVoiceTurn = {
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
      const sourceIds = event.source_utterance_ids ?? []
      liveVoiceTurn = {
        responseId: event.response_id,
        sourceUtteranceIds: sourceIds,
        userContent: sourceIds
          .map((utteranceId) => finalizedUtterances.get(utteranceId) ?? '')
          .filter((text) => text !== '')
          .join('\n'),
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
      && event.text_sequence === liveVoiceTurn.lastTextSequence + 1
    ) {
      liveVoiceTurn = {
        ...liveVoiceTurn,
        assistantContent: liveVoiceTurn.assistantContent + event.text,
        lastTextSequence: event.text_sequence,
      }
      return
    }
    if (
      ['response_completed', 'response_cancelled', 'response_failed'].includes(event.type)
      && liveVoiceTurn !== null
      && event.response_id === liveVoiceTurn.responseId
    ) {
      if (event.type === 'response_failed') {
        const context = conversationController.selectedContext()
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
      liveVoiceTurn = null
      if (event.type !== 'response_failed') {
        const context = conversationController.selectedContext()
        if (context !== null) {
          void conversationController.refreshTurns(context)
          void sidebarController.refreshCharacter(context.character)
        }
      }
      return
    }
    if (event.type === 'error') {
      if (event.utterance_id !== undefined) finalizedUtterances.delete(event.utterance_id)
      appendApplicationError()
      return
    }
    if (event.type === 'utterance_discarded' && event.utterance_id !== undefined) {
      finalizedUtterances.delete(event.utterance_id)
      if (liveVoiceTurn?.responseId === null) liveVoiceTurn = null
    }
  }

  $: interactionsDisabled = pendingRequest !== null
    || $conversationController.pending
    || $conversationController.deleteCandidate !== null
  $: syncVoiceSelection(
    $conversationController.character,
    $conversationController.selectedConversationId,
  )
  $: voiceRecorderDisabled = $conversationController.selectedConversationId === null
    || $conversationController.pending
    || $conversationController.deleteCandidate !== null
    || endingVoiceSession
    || voiceSnapshot.phase === 'connecting'
  $: voiceRecorderForceOff = $conversationController.selectedConversationId === null
    || endingVoiceSession
    || voiceSnapshot.phase === 'error'
    || voiceSnapshot.phase === 'ended'
    || voiceSnapshot.phase === 'reconnecting'
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

  function syncVoiceSelection(character: string, conversationId: string | null) {
    const active = voiceSnapshot.context
    if (active === null || endingVoiceSession) return
    if (active.characterId === character && active.conversationId === conversationId) return
    void endVoiceSession()
  }

  onMount(() => {
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
      compactQuery?.removeEventListener('change', updateLayout)
      viewport?.removeEventListener('resize', updateViewport)
      viewport?.removeEventListener('scroll', updateViewport)
      window.removeEventListener('resize', updateViewport)
      void voiceSession.end().catch(() => undefined)
    }
  })

  const handleSend = async (message: string) => {
    const text = message.trim()
    const context = conversationController.selectedContext()
    if (text.length === 0 || interactionsDisabled || context === null) return
    pendingRequest = 'text'
    applicationError = null
    try {
      if (voiceSnapshot.sessionId !== null || voiceSnapshot.phase === 'reconnecting') {
        activeUtteranceId = null
        await voiceSession.end().catch(() => undefined)
      }
      const response = await sendChatMessage({
        character: context.character,
        conversationId: context.conversationId,
        message: text,
      })
      conversationController.appendTurn(context, response.turn)
      void sidebarController.refreshCharacter(context.character)
    } catch {
      conversationController.reportConversationError(context)
    } finally {
      if (conversationController.selectedContext()?.version === context.version) pendingRequest = null
    }
  }

  const handleSelectConversation = async (character: string, conversationId: string) => {
    if (interactionsDisabled) return
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
    applicationError = null
    try {
      await voiceSession.ensureSession({
        characterId: context.character,
        conversationId: context.conversationId,
      })
    } catch (error) {
      appendApplicationError()
      throw error
    }
  }

  const resumeVoiceMicrophone = async (stream: MediaStream) => {
    try {
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
  {#if sidebarOpen}
    {#if compactLayout}
      <button class="drawer-backdrop" type="button" aria-label="サイドバーを閉じる" on:click={() => { sidebarOpen = false }}></button>
    {/if}
    <ConversationSidebar
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
      onOpenMemory={() => { showingMemoryManagement = true; if (compactLayout) sidebarOpen = false }}
    />
  {:else}
    <button class="floating-menu" type="button" aria-label="サイドバーを開く" on:click={() => { sidebarOpen = true }}>☰</button>
  {/if}
  {#if showingMemoryManagement}
    <section class="content-panel memory-panel">
      <MemoryManagement character={$conversationController.character} onClose={() => { showingMemoryManagement = false }} />
    </section>
  {:else}
  <section class="chat-panel" aria-label={`${currentCharacterEntry?.display_name ?? $conversationController.character}とのチャット`}>
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
          liveVoiceTurn={liveVoiceTurn}
        />
      </div>
    </div>
    {#if applicationError !== null || $conversationController.error !== null}
      <p class="application-error" role="alert">{applicationError ?? $conversationController.error}</p>
    {/if}
    {#if voiceSnapshot.phase !== 'idle'}
      <section class="voice-status" aria-label="音声会話の状態" aria-live="polite">
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
      <InputBar
        onSend={handleSend}
        characterName={currentCharacterEntry?.display_name ?? $conversationController.character}
        disabled={interactionsDisabled || $conversationController.selectedConversationId === null}
      />
      <AudioRecorder
        disabled={voiceRecorderDisabled}
        forceOff={voiceRecorderForceOff}
        continuous={true}
        onBeforeEnable={ensureVoiceSession}
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

  .floating-menu { position: fixed; z-index: 55; top: 14px; left: 14px; display: grid; width: 42px; height: 42px; place-items: center; border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 12px; color: #f8f3ff; background: rgba(27, 23, 38, 0.9); box-shadow: 0 10px 25px rgba(0, 0, 0, 0.28); cursor: pointer; backdrop-filter: blur(12px); }
  .floating-menu:focus-visible { outline: 2px solid #f0a3c1; outline-offset: 2px; }
  .drawer-backdrop { position: fixed; z-index: 45; inset: 0; border: 0; background: rgba(3, 2, 6, 0.62); }
  .memory-panel { overflow: auto; padding: 18px; }

  .input-area {
    display: flex;
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
    min-height: 36px;
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
