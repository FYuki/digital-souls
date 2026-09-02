<script lang="ts">
  import type { ConversationTurn } from './conversations/types'

  export let turns: ConversationTurn[]
  export let characterName = '光織'
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
        <span class="speaker">{characterName}</span>
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
      <span class="speaker">{characterName}（応答失敗）</span>
      <p>{turn.assistantContent || '応答を完了できませんでした。'}</p>
    </article>
  {/each}
  {#if liveVoiceTurn !== null}
    <article class="message user" data-live-voice-turn="true">
      <span class="speaker">あなた</span>
      <p>{liveVoiceTurn.userContent}</p>
    </article>
    <article class="message" data-live-voice-turn="true">
      <span class="speaker">{characterName}（応答中）</span>
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
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 12px;
    color: #eee8f3;
    background: #211b2a;
  }

  .message.user {
    align-self: flex-end;
    border-color: rgba(240, 163, 193, 0.22);
    background: #8d4260;
    color: #fff8fb;
  }

  .message.failed {
    border-color: rgba(255, 125, 125, 0.28);
    background: #382027;
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
