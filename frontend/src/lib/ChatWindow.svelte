<script lang="ts">
  import type { ConversationTurn } from './conversations/types'
  import {voiceDisplayTurns, type VoiceTurnDisplay, type SettledVoiceTurnDisplay,
    type FailedVoiceTurnDisplay} from './voice-turn-display'

  export let turns: ConversationTurn[]
  export let characterName = '光織'
  export let failedVoiceTurns: FailedVoiceTurnDisplay[] = []
  export let liveVoiceTurn: VoiceTurnDisplay | null = null
  export let settledVoiceTurns: SettledVoiceTurnDisplay[] = []
  $: displayedTurns = voiceDisplayTurns(turns, failedVoiceTurns, settledVoiceTurns, liveVoiceTurn)
</script>

<div class="messages" aria-live="polite">
  {#each displayedTurns as turn (turn.key)}
    {#if turn.kind === 'content'}
      <article class="message user" data-turn-id={turn.turnId}
        data-live-voice-turn={turn.voicePending ? 'true' : undefined} data-failed-voice-turn={turn.failedResponseId}>
        <span class="speaker">あなた</span>
        <p>{turn.userContent}</p>
      </article>
      <article class="message" class:failed={turn.failedResponseId !== undefined} data-turn-id={turn.turnId}
        data-live-voice-turn={turn.voicePending ? 'true' : undefined} data-failed-voice-turn={turn.failedResponseId}>
        <span class="speaker">{characterName}{turn.label ? `（${turn.label}）` : ''}</span>
        <p data-live-response-text={turn.liveResponseId} data-history-turn-text={turn.historyTurnId}>{turn.assistantContent}</p>
      </article>
    {:else}
      <article class="message privacy" data-turn-id={turn.turnId}>
        <span class="speaker">保存されなかったターン</span>
        <p>{turn.reason}</p>
      </article>
    {/if}
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
