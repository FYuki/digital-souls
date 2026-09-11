<script lang="ts">
  import { onMount } from 'svelte'
  import { approvalApi, choiceLabel, groupLabel, sceneLabel, permissionLabel, parseRequests, parseSettings,
    type ApprovalChoice, type ApprovalRequest, type ApprovalSetting, type Permission } from './addon-admin/approvals'

  export let onContinued: (item: ApprovalRequest, body: Record<string, unknown>) => void = () => undefined
  const choices: ApprovalChoice[] = ['always', 'once', 'reject']
  let requests: ApprovalRequest[] = []
  let settings: ApprovalSetting[] = []
  let next: string | null = null
  let includeAnswered = false
  let loading = false
  let busy = false
  let ended = false
  let error = ''
  let notice = ''
  let retryItem: ApprovalRequest | null = null
  let now = Date.now() / 1000
  const controller = new AbortController()
  const waiting = (item: ApprovalRequest) => item.waiting && now < item.wait_until

  async function refresh(more = false) {
    if (loading || ended) return
    loading = true
    error = ''
    try {
      const query = new URLSearchParams({ unanswered: String(!includeAnswered) })
      if (more && next) query.set('before', next)
      const [queue, permissions] = await Promise.all([
        approvalApi(`requests?${query}`, { signal: controller.signal }),
        approvalApi('permissions', { signal: controller.signal }),
      ])
      const page = parseRequests(queue)
      const parsed = parseSettings(permissions)
      if (ended) return
      requests = more ? [...requests, ...page.requests.filter(item => !requests.some(old => old.id === item.id))] : page.requests
      next = page.next
      settings = parsed
    } catch {
      if (!ended) error = '承認情報を取得できませんでした。状態を再取得してください。'
    } finally { loading = false }
  }

  async function continueRequest(item: ApprovalRequest) {
    retryItem = null
    try {
      const body = await approvalApi(`requests/${encodeURIComponent(item.id)}/continue`, { method: 'POST' })
      onContinued(item, body)
      if (body.state === 'ended') notice = '元の操作の待機は終了しています。元の操作は再開しません。'
      else notice = '回答を保存しました。有効な待機中の操作を続行します。'
    } catch {
      retryItem = item
      error = '回答は保存しました。続行を確認できませんでした。音声の応答が終わってから再度続行できます。'
    }
  }

  async function answer(item: ApprovalRequest, choice: ApprovalChoice) {
    if (busy || loading) return
    busy = true
    error = ''; notice = ''; retryItem = null
    try {
      const result = await approvalApi(`requests/${encodeURIComponent(item.id)}/answer`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ choice }),
      })
      const saved = parseRequests({ requests: [result.request], next_cursor: null }).requests[0]
      if (waiting(saved)) await continueRequest(saved)
      else notice = choice === 'reject'
        ? '拒否を保存しました。元の操作は再開しません。'
        : '承認を保存しました。元の操作は再開せず、今後の利用に適用します。'
      const continuationError = error
      await refresh()
      if (continuationError) error = continuationError
    } catch {
      error = '回答の保存を確認できませんでした。状態を再取得して確認してください。'
    } finally { busy = false }
  }

  async function changeSetting(item: ApprovalSetting, permission: Permission) {
    if (busy || loading) return
    busy = true; error = ''; notice = ''; retryItem = null
    try {
      await approvalApi('permissions', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ connection_id: item.connection_id, connection_token: item.connection_token,
          operation_group: item.operation_group, scene: item.scene, permission }),
      })
      notice = permission === 'unapproved' ? '未承認へ戻しました。未使用の単回許可と要求への予約を取り消しました。' : '承認設定を保存しました。'
      await refresh()
    } catch { error = '設定の保存を確認できませんでした。状態を再取得してください。' }
    finally { busy = false }
  }

  async function retryContinue(item: ApprovalRequest) {
    if (busy) return
    busy = true; error = ''
    try { await continueRequest(item) } finally { busy = false }
  }

  onMount(() => {
    void refresh()
    const timer = setInterval(() => { now = Date.now() / 1000 }, 1000)
    return () => { ended = true; controller.abort(); clearInterval(timer) }
  })
</script>

<section aria-label="確認キュー・承認設定" class="approvals">
  <h2>確認キュー・承認設定</h2>
  <p>承認はすべてのキャラクターに共通で、接続・操作群・対話中／会話外ごとに適用します。接続のON/OFFや会話外の利用許可は別の設定です。</p>
  <div class="toolbar">
    <button type="button" disabled={busy || loading} on:click={() => refresh()}>承認状態を再取得</button>
    <label><input type="checkbox" checked={includeAnswered} disabled={busy || loading} on:change={(event) => { includeAnswered = event.currentTarget.checked; void refresh() }} />回答済みも表示</label>
  </div>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if notice}<p role="status">{notice}</p>{/if}
  {#if retryItem}<button type="button" disabled={busy || loading} on:click={() => retryItem && retryContinue(retryItem)}>保存した回答で続行を再試行</button>{/if}
  {#if loading}<p role="status">承認情報を読み込んでいます…</p>{/if}
  <h3>確認キュー</h3>
  {#if !loading && requests.length === 0 && !error}<p>確認要求はありません。</p>{/if}
  <ul aria-label="確認要求一覧">
    {#each requests as item (item.id)}
      <li>
        <h4>{item.preview.connection}：{item.preview.operation}</h4>
        <p>{groupLabel(item)}・{sceneLabel(item)}／要求元：{item.character_id}</p>
        <p>対象：{item.preview.target}</p>
        <p><time datetime={new Date(item.created_at * 1000).toISOString()}>{new Date(item.created_at * 1000).toLocaleString('ja-JP')}</time></p>
        <details><summary>操作内容を確認</summary><pre>{JSON.stringify(item.preview.arguments ?? {}, null, 2)}</pre></details>
        {#if !item.connection_available}<p>接続が削除または再登録されています。この要求には回答できません。</p>
        {:else if waiting(item)}<p>元の操作は待機中です。承認すると有効な待機を続行します。「一度」はこの要求の呼び出し1回分です。</p>
        {:else}<p>元の操作の待機は終了しています。後から承認しても再開せず、同じ承認範囲の将来の利用に適用します。「一度」は将来の呼び出し1回分です。</p>{/if}
        {#if item.choice === null}
          <div class="choices">
            {#each choices as choice}
              <button type="button" disabled={busy || loading || !item.connection_available} on:click={() => answer(item, choice)}>{choiceLabel(choice)}</button>
            {/each}
          </div>
          <p>拒否は{item.scene === 'conversation' ? 'この要求だけに適用し、次の利用時には再確認できます。' : '設定変更まで、この接続・操作群の会話外利用を止めます。'}</p>
        {:else}
          <p>回答済み：{choiceLabel(item.choice)}。現在の効力は下の承認設定で確認できます。</p>
          {#if item.once_reserved && waiting(item)}<p>この要求へ単回許可を1回分予約しています。</p>{/if}
          {#if waiting(item) && item.connection_available && item.scene === 'conversation'}
            <button type="button" disabled={busy || loading} on:click={() => retryContinue(item)}>保存した回答で続行</button>
          {/if}
        {/if}
      </li>
    {/each}
  </ul>
  {#if next}<button type="button" disabled={busy || loading} on:click={() => refresh(true)}>さらに表示</button>{/if}
  <h3>保存済み承認設定</h3>
  <p>「未承認へ戻す」は未使用の単回許可と要求への予約も取り消します。実行開始済みの操作は中断・巻き戻ししません。</p>
  <ul aria-label="保存済み承認設定">
    {#each settings as item (`${item.connection_token}:${item.operation_group}:${item.scene}`)}
      <li aria-label={`${item.connection_label}・${groupLabel(item)}・${sceneLabel(item)}`}>
        <h4>{item.connection_label}：{groupLabel(item)}・{sceneLabel(item)}</h4>
        <p>設定：{permissionLabel(item.permission)}</p>
        <p>将来の呼び出し用の単回許可：{item.remaining}回／待機中の要求への予約：{item.reserved}回</p>
        <div class="choices">
          <button type="button" disabled={busy || loading} on:click={() => changeSetting(item, 'always')}>常に承認へ変更</button>
          <button type="button" disabled={busy || loading} on:click={() => changeSetting(item, 'unapproved')}>未承認へ戻す</button>
          {#if item.scene === 'autonomous'}<button type="button" disabled={busy || loading} on:click={() => changeSetting(item, 'denied')}>拒否へ変更</button>{/if}
        </div>
      </li>
    {/each}
  </ul>
</section>

<style>
  .approvals { margin-top: 28px; border-top: 1px solid #65758d; padding-top: 18px; }
  h2 { font-size: 1.2rem; } h3 { margin-top: 24px; } h4 { margin: 0; overflow-wrap: anywhere; }
  p { color: #c2cddd; line-height: 1.6; overflow-wrap: anywhere; }
  ul { list-style: none; padding: 0; } li { border: 1px solid #65758d; border-radius: 8px; padding: 14px; margin: 12px 0; }
  .toolbar, .choices { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  button { min-height: 44px; padding: 10px; color: #e2e8f0; background: #263248; border: 1px solid #65758d; border-radius: 8px; cursor: pointer; }
  button:disabled { opacity: 0.6; cursor: wait; } button:focus-visible { outline: 2px solid #b8a5ff; outline-offset: 3px; }
  label { display: flex; gap: 8px; align-items: center; min-height: 44px; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 160px; overflow: auto; }
  [role="alert"] { color: #ffb5b5; }
</style>
