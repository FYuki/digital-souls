<script lang="ts">
  import { onMount } from 'svelte'

  import AudioPlayer from './lib/AudioPlayer.svelte'
  import AudioRecorder from './lib/AudioRecorder.svelte'
  import type { AudioCaptureMetadata } from './lib/AudioRecorder.svelte'
  import CharacterSwitcher from './lib/CharacterSwitcher.svelte'
  import ChatWindow from './lib/ChatWindow.svelte'
  import ConversationSidebar from './lib/ConversationSidebar.svelte'
  import HardDeleteDialog from './lib/HardDeleteDialog.svelte'
  import InputBar from './lib/InputBar.svelte'
  import MemoryManagement from './lib/MemoryManagement.svelte'
  import {
    WebSocketAudioTransport,
    type AudioTransport,
    type AudioResponseMetadata,
    type BackendErrorMessage,
    type TransportCallbacks,
  } from './lib/audio/transport'
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
    type SelectedConversationContext,
  } from './lib/conversations/controller'
  import type { ConversationTurn } from './lib/conversations/types'

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
  type PendingRequest = 'text' | 'audio' | null
  type AudioPlaybackRequest = {
    audioData: ArrayBuffer
    onFirstPlayback: () => void
  }

  let pendingRequest: PendingRequest = null
  let isConnected = false
  let playbackRequest: AudioPlaybackRequest | null = null
  let transport: AudioTransport | null = null
  let transportConversationKey: string | null = null
  let applicationError: string | null = null
  let transportSessionId: string | null = null
  let responseMetadata: AudioResponseMetadata | null = null
  let showingMemoryManagement = false

  $: interactionsDisabled = pendingRequest !== null
    || $conversationController.pending
    || $conversationController.deleteCandidate !== null
  $: syncTransport(
    $conversationController.character,
    $conversationController.selectedConversationId,
  )

  const appendApplicationError = () => {
    applicationError = ERROR_MESSAGE
  }

  const disconnectTransport = () => {
    const previous = transport
    transport = null
    transportConversationKey = null
    transportSessionId = null
    responseMetadata = null
    isConnected = false
    previous?.disconnect()
  }

  const resolveAudioWebSocketUrl = (character: string): string => {
    const { protocol, host } = window.location
    const webSocketProtocol = protocol === 'https:' ? 'wss:' : 'ws:'
    return `${webSocketProtocol}//${host}/ws/${encodeURIComponent(character)}`
  }

  const createTransportCallbacks = (
    context: SelectedConversationContext,
    isCurrent: () => boolean,
    sourceTransport: () => AudioTransport,
  ): TransportCallbacks => ({
    onTurnMessage: (turn: ConversationTurn) => {
      if (!isCurrent()) return
      conversationController.appendTurn(context, turn)
      if (turn.kind === 'privacy_skipped') pendingRequest = null
    },
    onAudioMessage: (audio: ArrayBuffer) => {
      if (!isCurrent()) return
      const metadata = responseMetadata
      responseMetadata = null
      const source = sourceTransport()
      if (metadata !== null) {
        source.sendMeasurementEvent({
          ...metadata,
          eventId: crypto.randomUUID(),
          name: 'client_audio_received',
          timestamp: performance.now(),
        })
      }
      playbackRequest = {
        audioData: audio,
        onFirstPlayback: () => {
          if (metadata === null || transport !== source) return
          source.sendMeasurementEvent({
            ...metadata,
            eventId: crypto.randomUUID(),
            name: 'first_playback',
            timestamp: performance.now(),
          })
        },
      }
      pendingRequest = null
    },
    onAudioResponseMetadata: (metadata: AudioResponseMetadata) => {
      if (!isCurrent()) return
      responseMetadata = metadata
    },
    onError: (_error: BackendErrorMessage) => {
      if (!isCurrent()) return
      appendApplicationError()
      pendingRequest = null
    },
    onTransportError: (_error: Error) => {
      if (!isCurrent()) return
      appendApplicationError()
      pendingRequest = null
    },
    onOpen: () => { if (isCurrent()) isConnected = true },
    onClose: () => {
      if (!isCurrent()) return
      isConnected = false
      if (pendingRequest === 'audio') pendingRequest = null
    },
  })

  function syncTransport(character: string, conversationId: string | null) {
    const nextKey = conversationId === null ? null : `${character}:${conversationId}`
    if (nextKey === transportConversationKey) return
    disconnectTransport()
    if (conversationId === null) return
    const context = conversationController.selectedContext()
    if (context === null) return
    let nextTransport: AudioTransport
    const nextSessionId = crypto.randomUUID()
    const callbacks = createTransportCallbacks(
      context,
      () => transport === nextTransport,
      () => nextTransport,
    )
    nextTransport = new WebSocketAudioTransport(
      resolveAudioWebSocketUrl(character),
      conversationId,
      callbacks,
    )
    transport = nextTransport
    transportSessionId = nextSessionId
    transportConversationKey = nextKey
    void nextTransport.connect().catch(() => {
      if (transport !== nextTransport) return
      appendApplicationError()
      isConnected = false
    })
  }

  onMount(() => {
    void conversationController.loadCharacter(INITIAL_CHARACTER_ID)
    return disconnectTransport
  })

  const handleSend = async (message: string) => {
    const text = message.trim()
    const context = conversationController.selectedContext()
    if (text.length === 0 || interactionsDisabled || context === null) return
    pendingRequest = 'text'
    applicationError = null
    try {
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

  const handleAudioCaptured = (
    pcmData: ArrayBuffer,
    capture: AudioCaptureMetadata,
  ) => {
    if (
      !isConnected
      || interactionsDisabled
      || transport === null
      || transportSessionId === null
    ) return
    pendingRequest = 'audio'
    try {
      responseMetadata = null
      transport.sendAudio(pcmData, {
        eventId: crypto.randomUUID(),
        sessionId: transportSessionId,
        utteranceId: crypto.randomUUID(),
        ...capture,
        responseDecisionClientMs: performance.now(),
      })
    } catch {
      appendApplicationError()
      pendingRequest = null
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
    <ChatWindow turns={$conversationController.turns} />
    {#if applicationError !== null || $conversationController.error !== null}
      <p class="application-error" role="alert">{applicationError ?? $conversationController.error}</p>
    {/if}
    <div class="input-area">
      <InputBar onSend={handleSend} disabled={interactionsDisabled || $conversationController.selectedConversationId === null} />
      <AudioRecorder
        disabled={interactionsDisabled || !isConnected}
        forceOff={!isConnected}
        onAudioCaptured={handleAudioCaptured}
        onError={appendApplicationError}
      />
    </div>
    <AudioPlayer
      request={playbackRequest}
      onError={appendApplicationError}
    />
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

  :global(.input-area .input-bar) {
    flex: 1;
    min-width: 0;
    padding: 0;
    border-top: 0;
    background: transparent;
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
