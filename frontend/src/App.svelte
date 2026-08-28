<script lang="ts">
  import { onMount } from 'svelte'

  import AudioRecorder from './lib/AudioRecorder.svelte'
  import type { SpeechActivity } from './lib/AudioRecorder.svelte'
  import CharacterSwitcher from './lib/CharacterSwitcher.svelte'
  import ChatWindow from './lib/ChatWindow.svelte'
  import ConversationSidebar from './lib/ConversationSidebar.svelte'
  import HardDeleteDialog from './lib/HardDeleteDialog.svelte'
  import InputBar from './lib/InputBar.svelte'
  import MemoryManagement from './lib/MemoryManagement.svelte'
  import { sendChatMessage } from './lib/chat/client'
  import { createConversationSessionManager } from './lib/conversation-session'
  import {
    archiveConversation,
    createConversation,
    hardDeleteConversation,
    listActiveConversations,
    listArchivedConversations,
    listConversationTurns,
    unarchiveConversation,
  } from './lib/conversations/client'
  import {
    createConversationController,
  } from './lib/conversations/controller'
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
  type PendingRequest = 'text' | null

  let pendingRequest: PendingRequest = null
  let applicationError: string | null = null
  let showingMemoryManagement = false
  let activeUtteranceId: string | null = null
  let endingVoiceSession = false
  type LiveVoiceTurn = {
    responseId: string | null
    sourceUtteranceIds: string[]
    userContent: string
    assistantContent: string
    lastTextSequence: number
  }
  let liveVoiceTurn: LiveVoiceTurn | null = null
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
      const persisted = event.type === 'response_completed'
        || event.type === 'response_cancelled'
      for (const utteranceId of liveVoiceTurn.sourceUtteranceIds) {
        finalizedUtterances.delete(utteranceId)
      }
      liveVoiceTurn = null
      if (persisted) {
        const context = conversationController.selectedContext()
        if (context !== null) void conversationController.refreshTurns(context)
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
    void conversationController.loadCharacter(INITIAL_CHARACTER_ID)
    return () => { void voiceSession.end().catch(() => undefined) }
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
        await voiceSession.end()
      }
      const response = await sendChatMessage({
        character: context.character,
        conversationId: context.conversationId,
        message: text,
      })
      conversationController.appendTurn(context, response.turn)
    } catch {
      conversationController.reportConversationError(context)
    } finally {
      if (conversationController.selectedContext()?.version === context.version) pendingRequest = null
    }
  }

  const handleCharacterSwitch = (character: string) => {
    if (interactionsDisabled || character === $conversationController.character) return
    void conversationController.loadCharacter(character)
  }

  const handleSelectConversation = (conversationId: string) => {
    if (interactionsDisabled) return
    void conversationController.selectConversation(conversationId)
  }

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

  const resumeVoiceMicrophone = async () => {
    try {
      await voiceSession.resumeMicrophone()
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

<main class="app-shell">
  {#if showingMemoryManagement}
    <MemoryManagement character={$conversationController.character} onClose={() => { showingMemoryManagement = false }} />
  {:else}
  <section class="chat-panel" aria-label="光織とのチャット">
    <header class="chat-header">
      <p class="eyebrow">digital-souls</p>
      <h1>光織</h1>
      <CharacterSwitcher currentCharacter={$conversationController.character} disabled={interactionsDisabled} onSwitch={handleCharacterSwitch} />
      <button type="button" on:click={() => { showingMemoryManagement = true }}>記憶管理</button>
    </header>
    <ConversationSidebar
      active={$conversationController.active}
      archived={$conversationController.archived}
      showingArchived={$conversationController.showingArchived}
      disabled={interactionsDisabled}
      onShowActive={conversationController.showActive}
      onShowArchived={conversationController.showArchived}
      onCreate={conversationController.createConversation}
      onSelect={handleSelectConversation}
      onArchive={conversationController.archiveConversation}
      onUnarchive={conversationController.unarchiveConversation}
      onDelete={conversationController.requestHardDelete}
    />
    <ChatWindow turns={$conversationController.turns} liveVoiceTurn={liveVoiceTurn} />
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
      <InputBar onSend={handleSend} disabled={interactionsDisabled || $conversationController.selectedConversationId === null} />
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

{#if $conversationController.deleteCandidate !== null}
  <HardDeleteDialog
    conversationId={$conversationController.deleteCandidate}
    disabled={$conversationController.pending}
    onConfirm={conversationController.confirmHardDelete}
    onCancel={conversationController.cancelHardDelete}
  />
{/if}

<style>
  .app-shell {
    min-height: 100vh;
    display: flex;
    align-items: stretch;
    justify-content: center;
    padding: 24px;
    box-sizing: border-box;
    background:
      linear-gradient(180deg, rgba(178, 73, 48, 0.1), rgba(255, 248, 243, 0) 35%),
      #fff8f3;
  }

  .chat-panel {
    width: min(880px, 100%);
    min-height: calc(100vh - 48px);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid rgba(144, 67, 47, 0.2);
    border-radius: 8px;
    background: rgba(255, 253, 250, 0.95);
    box-shadow: 0 18px 42px rgba(69, 39, 33, 0.12);
  }

  .chat-header {
    padding: 20px 24px 16px;
    border-bottom: 1px solid rgba(144, 67, 47, 0.16);
    background: #fff4ec;
  }

  .eyebrow {
    margin: 0 0 4px;
    color: #9f4933;
    font-size: 0.78rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  h1 {
    margin: 0;
    font-size: 1.6rem;
    color: #4a2822;
  }

  .input-area {
    display: flex;
    align-items: stretch;
    gap: 12px;
    padding: 16px 24px 20px;
    border-top: 1px solid rgba(144, 67, 47, 0.16);
    background: #fff4ec;
  }

  .application-error {
    margin: 0;
    padding: 8px 24px;
    color: #8a211b;
  }

  .voice-status {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 14px;
    padding: 10px 24px;
    border-top: 1px solid rgba(144, 67, 47, 0.12);
    color: #63382f;
    background: #fffaf6;
    font-size: 0.88rem;
  }

  .voice-status strong {
    flex-basis: 100%;
    font-weight: 600;
  }

  .voice-status button {
    min-height: 36px;
    border: 1px solid rgba(144, 67, 47, 0.28);
    border-radius: 8px;
    color: #6e3227;
    background: #fffdfa;
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
    border: 1px solid rgba(144, 67, 47, 0.28);
    border-radius: 8px;
    color: #6e3227;
    background: #fffdfa;
    font-weight: 700;
  }

  @media (max-width: 640px) {
    .app-shell {
      padding: 0;
    }

    .chat-panel {
      min-height: 100vh;
      border: 0;
      border-radius: 0;
    }

    .input-area {
      padding: 12px;
      gap: 8px;
    }
  }
</style>
