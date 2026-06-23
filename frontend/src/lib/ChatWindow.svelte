<script lang="ts">
  type ChatMessage = {
    id: number
    speaker: 'user' | 'miori'
    text: string
  }

  export let messages: ChatMessage[]
</script>

<div class="messages" aria-live="polite">
  {#each messages as message (message.id)}
    <article class:user={message.speaker === 'user'} class="message">
      <span class="speaker">{message.speaker === 'user' ? 'あなた' : '光織'}</span>
      <p>{message.text}</p>
    </article>
  {/each}
</div>

<style>
  .messages {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-height: 0;
    padding: 24px;
    overflow-y: auto;
  }

  .message {
    max-width: min(72%, 560px);
    align-self: flex-start;
    padding: 12px 14px;
    border: 1px solid rgba(144, 67, 47, 0.18);
    border-radius: 8px;
    background: #fff7f1;
  }

  .message.user {
    align-self: flex-end;
    background: #b94f38;
    color: #fffaf6;
  }

  .speaker {
    display: block;
    margin-bottom: 6px;
    font-size: 0.76rem;
    font-weight: 700;
  }

  p {
    margin: 0;
    line-height: 1.6;
    overflow-wrap: anywhere;
  }

  @media (max-width: 640px) {
    .messages {
      padding: 16px;
    }

    .message {
      max-width: 88%;
    }
  }
</style>
