<script lang="ts">
  import type { Conversation } from './conversations/types'

  export let active: Conversation[]
  export let archived: Conversation[]
  export let showingArchived: boolean
  export let disabled: boolean
  export let onShowActive: () => void
  export let onShowArchived: () => void
  export let onCreate: () => void
  export let onSelect: (conversationId: string) => void
  export let onArchive: (conversationId: string) => void
  export let onUnarchive: (conversationId: string) => void
  export let onDelete: (conversationId: string) => void

  $: visible = showingArchived ? archived : active
</script>

<aside class="threads" aria-label="スレッド一覧">
  <div class="thread-tabs">
    <button type="button" on:click={onShowActive} disabled={disabled}>アクティブ</button>
    <button type="button" on:click={onShowArchived} disabled={disabled}>アーカイブ済み</button>
  </div>
  {#if !showingArchived}
    <button type="button" class="create" on:click={onCreate} disabled={disabled}>新規スレッド</button>
  {/if}
  <ul>
    {#each visible as conversation (conversation.conversation_id)}
      <li>
        <button
          type="button"
          class="thread-id"
          on:click={() => onSelect(conversation.conversation_id)}
          disabled={disabled || showingArchived}
        >{conversation.conversation_id}</button>
        {#if showingArchived}
          <button type="button" on:click={() => onUnarchive(conversation.conversation_id)} disabled={disabled}>
            復元 {conversation.conversation_id}
          </button>
          <button type="button" on:click={() => onDelete(conversation.conversation_id)} disabled={disabled}>
            削除 {conversation.conversation_id}
          </button>
        {:else}
          <button type="button" on:click={() => onArchive(conversation.conversation_id)} disabled={disabled}>
            アーカイブ {conversation.conversation_id}
          </button>
        {/if}
      </li>
    {/each}
  </ul>
  {#if showingArchived}
    <p>アーカイブしても履歴は削除されずSQLiteに保持されます。RAG長期記憶とは別の操作です。</p>
  {/if}
</aside>

<style>
  .threads { padding: 12px 24px; border-bottom: 1px solid rgba(144, 67, 47, 0.16); }
  .thread-tabs { display: flex; gap: 8px; margin-bottom: 8px; }
  .create { margin-bottom: 8px; }
  ul { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
  li { display: flex; flex-wrap: wrap; gap: 6px; }
  button { padding: 5px 8px; border: 1px solid #b87868; border-radius: 5px; background: #fff; cursor: pointer; }
  button:disabled { cursor: not-allowed; opacity: 0.55; }
  .thread-id { overflow-wrap: anywhere; }
  p { margin: 8px 0 0; color: #6f3a2d; font-size: 0.8rem; }
</style>
