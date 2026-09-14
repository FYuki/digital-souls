<script lang="ts">
  import { onMount, tick } from 'svelte'
  import type { MemoryTime } from './memory/episodic-client'
  import { listSemanticMemories, correctSemanticMemory, deleteSemanticMemory,
    type SemanticMemory } from './memory/semantic-client'

  export let character: string
  export let onOpenConversation: (conversationId: string) => void = () => undefined
  let records: SemanticMemory[] = []
  let loading = true
  let busy = false
  let error = ''
  let editing: SemanticMemory | null = null
  let deleting: SemanticMemory | null = null
  let value = ''
  let receipt = ''
  let submittedValue = ''
  let editInput: HTMLInputElement
  let trigger: HTMLButtonElement | null = null
  const statusLabels = { ACTIVE: '有効', HISTORICAL: '過去の状態', SUPERSEDED: '訂正前',
    CONFLICTED: '矛盾を保留', INACTIVE: '利用停止', DELETED: '削除済み' }
  const relationLabels: Record<string, string> = {
    CORRECT: '訂正', CHANGE: '時間による変化', CONFLICT: '矛盾保留',
    SELF_REPORT: '自己申告を優先', REAFFIRM: '再言及', NEW: '新規',
  }
  const date = (raw: string) => new Date(raw).toLocaleString('ja-JP')
  const formatTime = (time: MemoryTime | null | undefined) => {
    if (!time) return '不明（取得日時では補いません）'
    const p = time.parts
    return [[p.year, '年'], [p.month, '月'], [p.day, '日'], [p.hour, '時'], [p.minute, '分']]
      .filter(([part]) => part !== null).map(([part, unit]) => `${part}${unit}`).join('') + `（${time.timezone}）`
  }
  async function load() {
    loading = true
    error = ''
    try { records = await listSemanticMemories(character) }
    catch (caught) { error = caught instanceof Error ? caught.message : '読み込みに失敗しました。' }
    finally { loading = false }
  }
  onMount(() => { void load() })
  async function beginEdit(record: SemanticMemory, button: HTMLButtonElement) {
    editing = record
    deleting = null
    value = record.proposition?.value ?? ''
    receipt = crypto.randomUUID()
    submittedValue = ''
    trigger = button
    error = ''
    await tick()
    editInput?.focus()
  }
  async function cancel() {
    editing = null
    deleting = null
    error = ''
    await tick()
    trigger?.focus()
  }
  async function save() {
    if (!editing || busy || !value.trim()) return
    if (submittedValue && submittedValue !== value.trim()) receipt = crypto.randomUUID()
    submittedValue = value.trim()
    busy = true
    error = ''
    try {
      await correctSemanticMemory(character, editing, submittedValue, receipt)
      editing = null
      records = []
      await load()
    } catch (caught) { error = caught instanceof Error ? caught.message : '訂正に失敗しました。' }
    finally { busy = false }
  }
  async function remove() {
    if (!deleting || busy) return
    busy = true
    error = ''
    try {
      await deleteSemanticMemory(character, deleting)
      deleting = null
      records = []
      await load()
    } catch (caught) { error = caught instanceof Error ? caught.message : '削除に失敗しました。' }
    finally { busy = false }
  }
  async function reload() {
    editing = null
    deleting = null
    records = []
    await load()
  }
</script>

<section class="semantic" aria-label="意味記憶">
  <div class="heading"><div><h2>意味記憶</h2><p>会話を離れて使う知識や好みを確認できます。</p></div>
    <button disabled={busy || loading} on:click={reload}>再読み込み</button></div>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if loading}<p role="status">意味記憶を読み込み中</p>
  {:else if !records.length && !error}<p>保存された意味記憶はありません。</p>{/if}
  {#each records as record (record.id)}
    <article id={`semantic-${record.id}`} aria-label={`意味記憶 ${record.id}`}>
      <div class="heading">
        <h3>{record.proposition?.predicate ?? '本文を利用できない記憶'}</h3>
        <span>{statusLabels[record.status]}・第{record.content_version}版</span>
      </div>
      {#if record.proposition}<p class="content">{record.proposition.content}</p>{/if}
      <p>{record.formation_type === 'DIRECT_EXTRACTION' ? '発言から直接取得' : '経験から一般化'}
        {record.proposition?.self_report ? '・自己申告' : ''} / 確信度 {Math.round(record.confidence * 100)}%</p>
      {#if record.status === 'CONFLICTED'}<p>根拠が食い違うため、どちらかを確定して会話に使いません。</p>{/if}
      {#if record.reassessment_pending}<p>出典の変化により、再評価が必要です。</p>{/if}
      <p class="metadata">作成：{date(record.created_at)} / 更新：{date(record.updated_at)}</p>
      <details>
        <summary>出典・適用時期・更新履歴</summary>
        <p>適用開始：{formatTime(record.proposition?.valid_from)}</p>
        <p>適用終了：{formatTime(record.proposition?.valid_until)}</p>
        {#each record.sources as source}
          <div class="source">
            {#if source.kind === 'CONVERSATION' && source.conversation_id}
              <button on:click={() => source.conversation_id && onOpenConversation(source.conversation_id)}>出典の会話を開く</button>
            {:else if source.kind === 'EPISODE'}
              <a href={`#episodic-${source.source_id}`}>根拠の経験を開く</a>
            {:else}<span>管理画面での訂正</span>{/if}
            <p class="metadata">出典 {source.source_id} / 第{source.revision}版
              {#if source.span} / {source.span.role} / 範囲 {source.span.start}–{source.span.end}{/if}</p>
          </div>
        {/each}
        {#each record.versions as version}
          <p class="metadata">第{version.content_version}版 / {date(version.created_at)}
            {version.content_erased ? '（本文削除済み）' : ''} / 根拠 {version.sources.length}件</p>
          {#each version.sources as source}
            <p class="metadata">出典 {source.source_id} / 第{source.revision}版
              {#if source.span} / {source.span.role} / 範囲 {source.span.start}–{source.span.end}{/if}</p>
          {/each}
        {/each}
        {#each record.relations as relation}
          <p>{relationLabels[relation.relation] ?? relation.relation}：
            <a href={`#semantic-${relation.source_id}`}>以前の記憶（第{relation.source_version}版）</a>
            → <a href={`#semantic-${relation.target_id}`}>後の記憶（第{relation.target_version}版）</a></p>
        {/each}
        <p class="metadata">privacy方針 {record.stamp.policy_version}</p>
        {#if record.stamp.extraction}
          <p class="metadata">抽出：{record.stamp.extraction.model_id} / {record.stamp.extraction.prompt_version}</p>
        {/if}
      </details>
      {#if editing?.id === record.id}
        <form on:submit|preventDefault={save} aria-label="意味記憶の訂正">
          <label>{record.proposition?.predicate}<input bind:this={editInput} bind:value maxlength="240" required /></label>
          <p>自己申告の値を訂正します。元の会話は変わりません。</p>
          <button disabled={busy || !value.trim()} type="submit">訂正を保存</button>
          <button disabled={busy} type="button" on:click={cancel}>キャンセル</button>
        </form>
      {:else if deleting?.id === record.id}
        <div class="delete-confirm" role="group" aria-label="意味記憶の削除確認">
          <p>この意味記憶と履歴の本文を削除します。元の会話と経験は残ります。</p>
          <button disabled={busy} on:click={remove}>完全に削除</button>
          <button disabled={busy} on:click={cancel}>キャンセル</button>
        </div>
      {:else}
        <div class="actions">
          {#if record.can_correct}<button disabled={busy} on:click={(event) => beginEdit(record, event.currentTarget)}>自己申告を訂正</button>{/if}
          {#if record.status !== 'DELETED'}<button disabled={busy} on:click={(event) => {
            deleting = record; editing = null; trigger = event.currentTarget; error = ''
          }}>削除</button>{/if}
        </div>
      {/if}
    </article>
  {/each}
</section>

<style>
  .semantic { margin-block: 24px; }
  .heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  h2, h3 { margin-block: 8px; }
  article { border: 1px solid #514558; border-radius: 10px; padding: 16px; margin-block: 12px; scroll-margin-top: 16px; }
  .content { font-size: 1.05rem; white-space: pre-wrap; overflow-wrap: anywhere; }
  .metadata { font-size: .8rem; opacity: .8; overflow-wrap: anywhere; }
  button, input { font: inherit; }
  button { padding: 7px 12px; cursor: pointer; }
  button:disabled { cursor: wait; opacity: .6; }
  .actions { display: flex; gap: 8px; margin-top: 12px; }
  form, .delete-confirm { padding: 12px; border: 1px solid #847091; margin-top: 12px; }
  label { display: flex; flex-direction: column; gap: 6px; }
  input { padding: 8px; }
  details { margin-block: 12px; }
  summary { cursor: pointer; }
  a { color: #cab8f0; }
  [role="alert"] { color: #ffb2aa; }
</style>
