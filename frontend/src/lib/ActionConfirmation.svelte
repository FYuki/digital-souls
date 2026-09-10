<script lang="ts">
  import { onMount } from 'svelte'

  export let character: string
  export let conversationId: string
  export let requestId: string
  export let onContinue: (requestId: string) => Promise<void | 'ended'>

  type Choice = 'always' | 'once' | 'reject'
  type Confirmation = {
    id: string; operation_group: 'normal' | 'high_impact'; scene: 'conversation' | 'autonomous'
    choice: Choice | null
    preview: { connection: string; operation: string; target: string; arguments: unknown }
  }
  let pending: Confirmation | null = null
  let busy = false
  let error = ''
  let completed = false
  let waitEnded = false
  let ended = false
  const controller = new AbortController()

  async function load() {
    try {
      const query = new URLSearchParams({ character, session_id: conversationId })
      const response = await fetch(`/api/addon-actions/requests?${query}`, { signal: controller.signal, cache: 'no-store' })
      if (!response.ok) throw new Error('confirmation unavailable')
      const body = await response.json()
      const item = Array.isArray(body.requests) ? body.requests.find((r: {id?: unknown}) => r.id === requestId) : null
      if (!item || !['normal', 'high_impact'].includes(item.operation_group)
        || !['conversation', 'autonomous'].includes(item.scene)
        || !['connection', 'operation', 'target'].every(key => typeof item.preview?.[key] === 'string')
        || ![null, 'always', 'once', 'reject'].includes(item.choice)) throw new Error('invalid confirmation')
      if (!ended) pending = item
    } catch {
      if (!ended) error = '確認内容を取得できませんでした。'
    }
  }

  async function continueConversation() {
    busy = true
    error = ''
    try {
      waitEnded = await onContinue(requestId) === 'ended'
      completed = true
    } catch {
      error = '回答は保存しました。会話の続行を確認できませんでした。音声の応答が終わってから再度続行できます。'
    } finally {
      busy = false
    }
  }

  async function answer(choice: Choice) {
    if (busy || !pending || pending.choice !== null) return
    busy = true
    error = ''
    try {
      const response = await fetch(`/api/addon-actions/requests/${encodeURIComponent(requestId)}/answer`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        signal: controller.signal,
        body: JSON.stringify({ character, session_id: conversationId, choice }),
      })
      if (!response.ok) throw new Error('answer failed')
      pending = { ...pending, choice }
      if (!ended) await continueConversation()
    } catch {
      if (!ended) error = '回答の保存を確認できませんでした。もう一度選択してください。'
    } finally {
      busy = false
    }
  }

  onMount(() => {
    void load()
    return () => { ended = true; controller.abort() }
  })
</script>

<section class="confirmation" aria-label="外部操作の承認">
  {#if pending}
    <strong>外部操作の承認</strong>
    <dl>
      <dt>接続</dt><dd>{pending.preview.connection}</dd>
      <dt>操作</dt><dd>{pending.preview.operation}</dd>
      <dt>対象</dt><dd>{pending.preview.target}</dd>
      <dt>承認の範囲</dt><dd>{pending.operation_group === 'high_impact' ? 'ハイリスク操作群' : '通常操作群'}・{pending.scene === 'conversation' ? '対話中' : '会話外'}</dd>
    </dl>
    <details><summary>操作内容を確認</summary><pre>{JSON.stringify(pending.preview.arguments, null, 2)}</pre></details>
    <p>承認はこの接続・操作群・実行場面に適用します。「一度」はツール呼び出し1回分です。音声の返答では承認されません。</p>
    {#if pending.choice === null}
      <div class="choices">
        <button type="button" disabled={busy} on:click={() => answer('always')}>常に承認する</button>
        <button type="button" disabled={busy} on:click={() => answer('once')}>一度承認する</button>
        <button type="button" disabled={busy} on:click={() => answer('reject')}>拒否する</button>
      </div>
    {:else if !completed}
      <button type="button" disabled={busy} on:click={continueConversation}>会話を続行</button>
    {/if}
    {#if busy}<p role="status">回答を反映しています…</p>{/if}
    {#if waitEnded}<p role="status">元の操作の待機は終了しています。保存した承認は今後の利用に適用します。</p>{/if}
  {:else if !error}
    <p role="status">確認内容を取得しています…</p>
  {/if}
  {#if error}<p role="alert">{error}</p>{/if}
</section>

<style>
  .confirmation { border: 1px solid #b8a681; border-radius: 0.5rem; padding: 0.75rem; margin: 0.5rem 0; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 0.25rem 0.75rem; }
  dd { margin: 0; overflow-wrap: anywhere; }
  p { max-width: 48rem; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 12rem; overflow: auto; }
  .choices { display: flex; flex-wrap: wrap; gap: 0.5rem; }
  button { min-height: 2.75rem; padding: 0.4rem 0.75rem; border: 1px solid #8f789a; border-radius: 0.4rem; background: #33263e; color: #f7edf9; cursor: pointer; }
  button:focus-visible { outline: 2px solid #efc9dc; outline-offset: 3px; }
  button:disabled { opacity: 0.6; cursor: wait; }
</style>
