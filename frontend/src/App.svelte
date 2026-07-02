<script lang="ts">
  import { onMount } from 'svelte'

  import AudioPlayer from './lib/AudioPlayer.svelte'
  import AudioRecorder from './lib/AudioRecorder.svelte'
  import ChatWindow from './lib/ChatWindow.svelte'
  import InputBar from './lib/InputBar.svelte'
  import {
    WebSocketAudioTransport,
    type AudioTransport,
    type BackendErrorMessage,
    type TextMessageSpeaker,
    type TransportCallbacks,
  } from './lib/audio/transport'

  type ChatMessage = {
    id: number
    speaker: 'user' | 'miori'
    text: string
  }

  const AUDIO_WS_PATH = '/ws/miori'
  const ERROR_MESSAGE = '応答の取得に失敗しました。'
  type PendingRequest = 'text' | 'audio' | null
  let messages: ChatMessage[] = []
  let nextMessageId = 1
  let pendingRequest: PendingRequest = null
  let isConnected = false
  let audioData: ArrayBuffer | null = null
  let transport: AudioTransport | null = null

  const createMessage = (speaker: ChatMessage['speaker'], text: string): ChatMessage => {
    const message = {
      id: nextMessageId,
      speaker,
      text,
    }
    nextMessageId += 1
    return message
  }

  const appendMessage = (message: ChatMessage) => {
    messages = [...messages, message]
  }

  const appendErrorMessage = (error: unknown) => {
    if (!(error instanceof Error)) {
      throw error
    }

    appendMessage(createMessage('miori', ERROR_MESSAGE))
  }

  const getTransport = (): AudioTransport => {
    if (transport === null) {
      throw new Error('Audio transport is not initialized')
    }

    return transport
  }

  const resolveAudioWebSocketUrl = (): string => {
    const { protocol, host } = window.location
    const webSocketProtocol = protocol === 'https:' ? 'wss:' : 'ws:'

    return `${webSocketProtocol}//${host}${AUDIO_WS_PATH}`
  }

  const handleTransportError = (_error: BackendErrorMessage) => {
    appendErrorMessage(new Error('WebSocket transport reported an error'))
    clearPendingRequest()
  }

  const handleTransportRuntimeError = (error: Error) => {
    appendErrorMessage(error)
    clearPendingRequest()
  }

  const clearPendingRequest = () => {
    pendingRequest = null
  }

  const clearAudioPendingRequest = () => {
    if (pendingRequest === 'audio') {
      clearPendingRequest()
    }
  }

  const createTransportCallbacks = (isCurrentTransport: () => boolean): TransportCallbacks => ({
    onTextMessage: (speaker: TextMessageSpeaker, text: string) => {
      if (!isCurrentTransport()) {
        return
      }

      appendMessage(createMessage(speaker, text))
      if (pendingRequest === 'text' && speaker === 'miori') {
        clearPendingRequest()
      }
    },
    onAudioMessage: (audio: ArrayBuffer) => {
      if (!isCurrentTransport()) {
        return
      }

      audioData = audio
      clearAudioPendingRequest()
    },
    onError: (error: BackendErrorMessage) => {
      if (!isCurrentTransport()) {
        return
      }

      handleTransportError(error)
    },
    onTransportError: (error: Error) => {
      if (!isCurrentTransport()) {
        return
      }

      handleTransportRuntimeError(error)
    },
    onOpen: () => {
      if (!isCurrentTransport()) {
        return
      }

      isConnected = true
    },
    onClose: () => {
      if (!isCurrentTransport()) {
        return
      }

      isConnected = false
      clearPendingRequest()
    },
  })

  const connectTransport = () => {
    let nextTransport: AudioTransport
    const callbacks = createTransportCallbacks(() => transport === nextTransport)
    nextTransport = new WebSocketAudioTransport(resolveAudioWebSocketUrl(), callbacks)
    transport = nextTransport
    void nextTransport.connect().catch((error) => {
      if (transport !== nextTransport) {
        return
      }

      appendErrorMessage(error)
      isConnected = false
      clearPendingRequest()
    })
  }

  onMount(() => {
    connectTransport()

    return () => {
      transport?.disconnect()
    }
  })

  const handleSend = (message: string) => {
    const text = message.trim()

    if (text.length === 0 || pendingRequest !== null) {
      return
    }

    appendMessage(createMessage('user', text))
    pendingRequest = 'text'
    try {
      getTransport().sendText(text)
    } catch (error) {
      appendErrorMessage(error)
      clearPendingRequest()
    }
  }

  const handleAudioCaptured = (pcmData: ArrayBuffer) => {
    if (!isConnected || pendingRequest !== null) {
      return
    }

    pendingRequest = 'audio'
    try {
      getTransport().sendAudio(pcmData)
    } catch (error) {
      appendErrorMessage(error)
      clearAudioPendingRequest()
    }
  }

  const handleAudioError = (error: Error) => {
    appendErrorMessage(error)
    clearAudioPendingRequest()
  }
</script>

<main class="app-shell">
  <section class="chat-panel" aria-label="光織とのチャット">
    <header class="chat-header">
      <p class="eyebrow">digital-souls</p>
      <h1>光織</h1>
    </header>

    <ChatWindow {messages} />
    <div class="input-area">
      <InputBar onSend={handleSend} disabled={pendingRequest !== null || !isConnected} />
      <AudioRecorder
        disabled={pendingRequest !== null || !isConnected}
        forceOff={!isConnected}
        onAudioCaptured={handleAudioCaptured}
        onError={handleAudioError}
      />
    </div>
    <AudioPlayer
      {audioData}
      onError={handleAudioError}
    />
  </section>
</main>

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
