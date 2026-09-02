<script lang="ts">
  import type { ConversationTurn } from './conversations/types'

  export let turns: ConversationTurn[]
  export let failedVoiceTurns: {
    responseId: string
    userContent: string
    assistantContent: string
  }[] = []
  export let liveVoiceTurn: {
    userContent: string
    assistantContent: string
  } | null = null
</script>

<div class="messages" aria-live="polite">
  {#each turns as turn (turn.turn_id)}
    {#if turn.kind === 'content'}
      <article class="message user" data-turn-id={turn.turn_id}>
        <span class="speaker">あなた</span>
        <p>{turn.user_content}</p>
      </article>
      <article class="message" data-turn-id={turn.turn_id}>
        <span class="speaker">光織</span>
        <p>{turn.assistant_content}</p>
      </article>
    {:else}
      <article class="message privacy" data-turn-id={turn.turn_id}>
        <span class="speaker">保存されなかったターン</span>
        <p>{turn.reason_code}</p>
      </article>
    {/if}
  {/each}
  {#each failedVoiceTurns as turn (turn.responseId)}
    <article class="message user" data-failed-voice-turn={turn.responseId}>
      <span class="speaker">あなた</span>
      <p>{turn.userContent}</p>
    </article>
    <article class="message failed" data-failed-voice-turn={turn.responseId}>
      <span class="speaker">光織（応答失敗）</span>
      <p>{turn.assistantContent || '応答を完了できませんでした。'}</p>
    </article>
  {/each}
  {#if liveVoiceTurn !== null}
    <article class="message user" data-live-voice-turn="true">
      <span class="speaker">あなた</span>
      <p>{liveVoiceTurn.userContent}</p>
    </article>
    <article class="message" data-live-voice-turn="true">
      <span class="speaker">光織（応答中）</span>
      <p>{liveVoiceTurn.assistantContent}</p>
    </article>
  {/if}
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

  .message.failed {
    border-color: rgba(153, 27, 27, 0.28);
    background: #fff1f1;
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
