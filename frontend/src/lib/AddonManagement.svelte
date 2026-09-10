<script lang="ts">
  import { onMount } from 'svelte'
  import ExternalConnectionEditor from './ExternalConnectionEditor.svelte'
  import { stateMessage, confirmationMessage } from './addon-admin/client'
  import { sortedAddons, type AddonController } from './addon-admin/controller'

  export let controller: AddonController
  export let onClose: () => void
  let heading: HTMLHeadingElement
  let editing: string | null | undefined = undefined
  $: items = sortedAddons($controller.items)
  onMount(() => { heading.focus(); void controller.refresh() })
</script>

<div class="management">
  <header>
    <h1 bind:this={heading} tabindex="-1">Addon / 連携</h1>
    <button type="button" on:click={onClose}>チャットへ戻る</button>
  </header>
  {#if editing !== undefined}
    {#key editing}<ExternalConnectionEditor connectionId={editing} onChanged={() => { void controller.refresh() }}
      onClose={() => { editing = undefined; void controller.refresh() }} />{/key}
  {:else}
  <p class="description">登録済みの連携を管理します。ON/OFFはすべてのキャラクターに共通です。</p>
  <p class="description">OFFにすると新しい利用と回答待ちを停止します。すでに開始した外部処理は取り消されない場合があります。</p>
  <div class="toolbar"><button type="button" on:click={() => { editing = null }}>外部MCPを追加</button><button type="button" on:click={() => controller.refresh()}>状態を再取得</button></div>
  {#if $controller.error}<p role="alert">{$controller.error}</p>{/if}
  {#if $controller.loading}
    <p role="status">連携を読み込んでいます</p>
  {:else if items.length === 0 && !$controller.error}
    <p role="status">登録済みの連携はありません</p>
  {:else}
    <ul aria-label="登録済みの連携">
      {#each items as item (item.connection_instance_id)}
        <li aria-busy={$controller.pending.has(item.connection_instance_id)}>
          <div class="details">
            <h2>{item.display_name}</h2>
            <span class="source">{item.source_type === 'external' ? 'External' : '自作'}</span>
          </div>
          <div class="connection-state">
            <p class:problem={item.desired_enabled && item.availability === 'unavailable'}
              class:warning={item.desired_enabled && item.availability === 'degraded'} aria-live="polite">{stateMessage(item)}</p>
            {#if item.settings_revision !== undefined}
              <p>{confirmationMessage(item)}</p>
            {/if}
          </div>
          {#if item.source_type === 'external'}
            <button type="button" class="edit" aria-label={`${item.display_name}の詳細・編集`} on:click={() => { editing = item.connection_instance_id }}>詳細・編集</button>
          {/if}
          <button type="button" class="toggle" role="switch"
            aria-label={`${item.display_name}を利用する`}
            aria-checked={item.desired_enabled}
            aria-disabled={$controller.pending.has(item.connection_instance_id)}
            on:click={() => controller.toggle(item.connection_instance_id, !item.desired_enabled)}>
            {item.desired_enabled ? 'ON' : 'OFF'}
          </button>
          {#if $controller.rowErrors[item.connection_instance_id]}
            <div class="row-error">
              <p role="alert">{$controller.rowErrors[item.connection_instance_id]}</p>
              <button type="button" on:click={() => controller.retry(item.connection_instance_id)}
                aria-label={`${item.display_name}の設定保存を再試行`}>設定保存を再試行</button>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
  {/if}
</div>

<style>
  .management { width: 100%; max-width: 820px; margin: 0 auto; color: #e2e8f0; container-type: inline-size; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  h1 { font-size: 1.35rem; margin: 0; }
  h1:focus { outline: none; }
  h2 { font-size: 1rem; margin: 0; overflow-wrap: anywhere; line-height: 1.5; }
  .description { color: #a8b4c7; line-height: 1.65; }
  .toolbar { margin: 20px 0; display: flex; gap: 8px; flex-wrap: wrap; }
  button { color: #e2e8f0; background: #263248; border: 1px solid #65758d; border-radius: 8px; padding: 10px 14px; cursor: pointer; min-height: 44px; }
  button:focus-visible { outline: 2px solid #b8a5ff; outline-offset: 3px; }
  button[aria-disabled="true"] { opacity: 0.6; cursor: wait; }
  ul { list-style: none; padding: 0; }
  li { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto 64px; align-items: center; gap: 12px 24px; padding: 18px 0; border-bottom: 1px solid #354056; }
  .details, .connection-state { display: grid; gap: 6px; min-width: 0; overflow-wrap: anywhere; }
  .connection-state p { margin: 0; line-height: 1.5; }
  .connection-state p + p { color: #a8b4c7; font-size: 0.875rem; }
  .source { justify-self: start; font-size: 0.75rem; border: 1px solid #65758d; border-radius: 5px; padding: 2px 7px; }
  .edit { grid-column: 3; white-space: nowrap; }
  .toggle { grid-column: 4; width: 64px; }
  .row-error { grid-column: 1 / -1; min-width: 0; overflow-wrap: anywhere; }
  .toggle[aria-checked="true"] { background: #514483; }
  .problem, [role="alert"] { color: #ffb5b5; }
  .warning { color: #f9d484; }
  @container (max-width: 560px) {
    li { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px 16px; }
    .edit { grid-column: 1; grid-row: 2; justify-self: start; }
    .toggle { grid-column: 2; grid-row: 2; justify-self: end; }
  }
  @media (max-width: 900px) { h1 { margin-inline-start: 54px; } }
</style>
