<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import { detailMessage, type Detail, type Notification, type NotificationController } from './notifications/client'
  export let controller: NotificationController
  export let onClose: () => void
  let heading: HTMLHeadingElement
  let selected: Notification | null = null
  let detail: Detail | null = null
  let fetching = false
  let request: AbortController | null = null
  let showingSettings = false
  $: sources = [...new Set($controller.sources.map(s => s.source_id))]
  $: characters = [...new Set($controller.sources.map(s => s.character_id))]
  $: settings = $controller.sources.filter((s, i, all) => all.findIndex(x => x.source_id === s.source_id && x.event_type === s.event_type) === i)
  $: if (selected && !$controller.loading && !$controller.items.some(item => item.id === selected?.id)) closeDetail()
  function closeDetail() { request?.abort(); request = null; selected = null; detail = null; fetching = false }
  async function openDetail(item: Notification) {
    closeDetail()
    selected = item
    fetching = true
    const active = new AbortController()
    request = active
    try {
      const result = await controller.detail(item, active.signal)
      if (request === active && !active.signal.aborted) detail = result
    } catch {
      if (request === active && !active.signal.aborted) detail = { state: 'unavailable' }
    } finally { if (request === active) fetching = false }
  }
  function visibility() { if (document.hidden) closeDetail() }
  onMount(() => { heading.focus(); void controller.refresh(); document.addEventListener('visibilitychange', visibility) })
  onDestroy(() => { closeDetail(); document.removeEventListener('visibilitychange', visibility) })
  const date = (seconds: number) => new Date(seconds * 1000).toLocaleString('ja-JP')
  const typeName = (type: string) => ({ 'task.completed': '処理完了', 'task.failed': '処理失敗', 'resource.updated': '更新' }[type] ?? type)
</script>

<div class="notifications">
  <header><h1 bind:this={heading} tabindex="-1">通知 <span class="badge">{$controller.unread_count}件未読</span></h1>
    <button on:click={onClose}>チャットへ戻る</button></header>
  <p class="description">保存から{$controller.retention.days}日間、最大{$controller.retention.max_per_user.toLocaleString()}件を表示します。</p>
  <div class="toolbar">
    <button on:click={() => { showingSettings = !showingSettings }}>通知設定</button>
    <button on:click={() => controller.refresh()} disabled={$controller.loading}>再取得</button>
  </div>
  {#if showingSettings}
    <section aria-label="通知設定" class="settings">
      <h2>受け取る通知</h2>
      <p class="description">通知をOFFにしても、元の処理や監視は続きます。再ON後に届く新しい分から通知します。</p>
      {#each settings as source}
        <div class="setting-row">
          <span>{source.source_id} · {typeName(source.event_type)}</span>
          <button role="switch" aria-label={source.source_id + ' ' + typeName(source.event_type) + 'の通知'}
            aria-checked={source.enabled} disabled={$controller.pending || !source.configurable || (!source.enabled && !source.available)}
            on:click={() => controller.preference(source, !source.enabled)}>{source.enabled ? 'ON' : 'OFF'}</button>
          {#if !source.available}<small>現在利用できません</small>{/if}
        </div>
      {:else}<p>設定できる通知元はありません。</p>{/each}
    </section>
  {/if}
  <div class="filters">
    <label>通知元<select aria-label="通知元" value={$controller.filters.source_id} on:change={(e) => { closeDetail(); controller.filter({ source_id: e.currentTarget.value }) }}><option value="">すべて</option>{#each sources as source}<option value={source}>{source}</option>{/each}</select></label>
    <label>担当<select aria-label="担当" value={$controller.filters.character_id} on:change={(e) => { closeDetail(); controller.filter({ character_id: e.currentTarget.value }) }}><option value="">すべて</option>{#each characters as character}<option value={character}>{character}</option>{/each}</select></label>
    <label class="check"><input type="checkbox" checked={$controller.filters.unread} on:change={(e) => { closeDetail(); controller.filter({ unread: e.currentTarget.checked, hidden: false }) }}>未読のみ</label>
    <label class="check"><input type="checkbox" checked={$controller.filters.hidden} on:change={(e) => { closeDetail(); controller.filter({ hidden: e.currentTarget.checked, unread: false }) }}>非表示の通知</label>
  </div>
  {#if $controller.retention.history_incomplete}
    <p class="notice" role="status">保持期間内でも、件数上限により削除された通知があります。元の処理や監視は取り消されません。</p>
  {/if}
  {#if $controller.sources.some(s => s.history_incomplete)}
    <p class="notice" role="status">提供元から復元できなかったイベント、または安全に通知できなかったイベントがあります。履歴はすべて揃っていません。</p>
  {/if}
  {#if $controller.sources.some(s => s.status === 'unavailable')}
    <p class="notice" role="status">一部の通知元から新着を確認できていません。</p>
  {/if}
  {#if $controller.error}<p role="alert">{$controller.error}</p>{/if}
  {#if selected}
    <section class="detail" aria-label="通知の詳細">
      <header><h2>{typeName(selected.event_type)} · {selected.source_id}</h2><button on:click={closeDetail}>詳細を閉じる</button></header>
      {#if fetching}<p role="status">提供元から取得しています…</p>
      {:else if detail?.state === 'available'}
        <p class="description">{selected.kind === 'monitor' ? '提供元の最新状態です。' : 'この通知に対応する実行結果です。'}</p>
        <pre>{detail.text}</pre>
        {#if detail.omitted}<p>内容の一部を省略しています。</p>{/if}
        <button disabled={$controller.pending} on:click={() => { const current = $controller.items.find(i => i.id === selected?.id); if (current) void controller.setState(current, 'read') }}>確認して既読にする</button>
      {:else if detail}
        <p role="status">{detailMessage(detail.state)}</p>
        <button on:click={() => { if (selected) void openDetail(selected) }}>取得を再試行</button>
      {/if}
    </section>
  {/if}
  <ul aria-label="通知一覧" aria-busy={$controller.loading}>
    {#each $controller.items as item (item.id)}
      <li class:unread={item.state === 'unread'}>
        <div class="item-heading"><h2>{typeName(item.event_type)}</h2><span>{item.state === 'unread' ? '未読' : item.state === 'hidden' ? '非表示' : '既読'}</span></div>
        <p>{item.source_id} · 担当 {item.character_id}</p>
        <small>保存 {date(item.created)}{#if item.metadata.occurred_at} · 発生 {new Date(item.metadata.occurred_at).toLocaleString('ja-JP')}{/if}</small>
        <div class="actions">
          <button on:click={() => openDetail(item)} aria-label={typeName(item.event_type) + ' ' + (item.metadata.task_ref ?? item.id) + 'の詳細'}>詳細を確認</button>
          <button disabled={$controller.pending} on:click={() => controller.setState(item, item.state === 'unread' ? 'read' : 'unread')}>{item.state === 'unread' ? '既読にする' : '未読にする'}</button>
          {#if item.state !== 'hidden'}<button disabled={$controller.pending} on:click={() => controller.setState(item, 'hidden')}>非表示にする</button>{/if}
        </div>
      </li>
    {:else}{#if !$controller.loading && !$controller.error}<li class="empty">該当する通知はありません。</li>{/if}{/each}
  </ul>
  <nav aria-label="通知のページ" class="toolbar">
    {#if $controller.filters.offset > 0}<button on:click={() => { closeDetail(); controller.filter({ offset: Math.max(0, $controller.filters.offset - 50) }) }}>前の通知</button>{/if}
    {#if $controller.next_offset !== null}<button on:click={() => { closeDetail(); controller.filter({ offset: $controller.next_offset ?? 0 }) }}>次の通知</button>{/if}
  </nav>
</div>

<style>
  .notifications { width: 100%; box-sizing: border-box; max-width: 900px; margin: 0 auto; color: #e2e8f0; }
  header, .toolbar, .filters, .actions, .setting-row, .item-heading { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  header, .item-heading { justify-content: space-between; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1rem; margin: 0; } h1:focus { outline: none; }
  .badge { font-size: .8rem; color: #c4b5fd; }
  .description, small { color: #a8b4c7; line-height: 1.7; } .toolbar, .filters { margin: 20px 0; }
  button, select { min-height: 44px; background: #263248; color: #e2e8f0; border: 1px solid #65758d; border-radius: 8px; padding: 8px 12px; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; } button:focus-visible, select:focus-visible { outline: 2px solid #b8a5ff; outline-offset: 2px; }
  label { display: flex; flex-direction: column; gap: 6px; } label.check { flex-direction: row; align-items: center; min-height: 44px; }
  .settings, .detail { background: #1c273a; padding: 20px; border: 1px solid #47556d; border-radius: 12px; margin: 16px 0; }
  .setting-row { padding: 12px 0; } .setting-row span { flex: 1; overflow-wrap: anywhere; }
  ul { list-style: none; padding: 0; } li { border-bottom: 1px solid #354056; padding: 20px 16px; overflow-wrap: anywhere; }
  li.unread { border-left: 3px solid #b8a5ff; background: #24233a; } .actions { margin-top: 14px; }
  .notice { color: #f2cd8a; line-height: 1.7; } pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.8; }
  @media (max-width: 900px) { .notifications > header { padding-left: 52px; } }
  .empty { color: #a8b4c7; text-align: center; padding: 48px 12px; }
</style>
