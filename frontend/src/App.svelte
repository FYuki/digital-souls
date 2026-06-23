<script lang="ts">
  import ChatWindow from './lib/ChatWindow.svelte'
  import InputBar from './lib/InputBar.svelte'
  import { sendMessage } from './lib/api'

  type ChatMessage = {
    id: number
    speaker: 'user' | 'miori'
    text: string
  }

  const ERROR_MESSAGE = '応答の取得に失敗しました。'

  let messages: ChatMessage[] = []
  let nextMessageId = 1
  let isSending = false

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

  const handleSend = async (message: string) => {
    const text = message.trim()

    if (text.length === 0 || isSending) {
      return
    }

    appendMessage(createMessage('user', text))
    isSending = true

    try {
      const response = await sendMessage(text)
      appendMessage(createMessage('miori', response))
    } catch (error) {
      appendErrorMessage(error)
    } finally {
      isSending = false
    }
  }
</script>

<main class="app-shell">
  <section class="chat-panel" aria-label="光織とのチャット">
    <header class="chat-header">
      <p class="eyebrow">digital-souls</p>
      <h1>光織</h1>
    </header>

    <ChatWindow {messages} />
    <InputBar onSend={handleSend} disabled={isSending} />
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

  @media (max-width: 640px) {
    .app-shell {
      padding: 0;
    }

    .chat-panel {
      min-height: 100vh;
      border: 0;
      border-radius: 0;
    }
  }
</style>
