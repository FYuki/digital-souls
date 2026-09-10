<script lang="ts">
  import { onMount, onDestroy } from 'svelte'
  import { checkConnection, checkErrorMessage, confirmationMessage, deleteConnection, getConnection, managementError,
    saveConnection, saveCredential, stateMessage, type ConnectionDetail, type ConnectionInput } from './addon-admin/client'

  export let connectionId: string | null = null
  export let onClose: () => void
  export let onChanged: () => void
  let currentId = connectionId
  let detail: ConnectionDetail | null = null
  let displayName = ''
  let transport: 'streamable_http' | 'stdio' = 'streamable_http'
  let endpoint = ''
  let auth: 'none' | 'bearer' = 'none'
  let command = ''
  let args: string[] = []
  let credential = ''
  let busy = false
  let loading = Boolean(connectionId)
  let error = ''
  let notice = ''
  let confirmingDelete = false
  let heading: HTMLHeadingElement
  let active = true
  let epoch = 0
  let reading = false
  let loaded = false
  let timer: ReturnType<typeof setInterval>
  const requests = new Set<AbortController>()

  function input(): ConnectionInput {
    return { display_name: displayName, settings: transport === 'streamable_http'
      ? { transport, endpoint, auth } : { transport, command, args: [...args] } }
  }
  function populate(value: ConnectionDetail) {
    detail = value
    displayName = value.display_name
    transport = value.settings.transport
    if (value.settings.transport === 'streamable_http') {
      endpoint = value.settings.endpoint; auth = value.settings.auth
    } else { command = value.settings.command; args = [...value.settings.args] }
    loaded = true
  }
  async function bounded<T>(operation: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const abort = new AbortController()
    requests.add(abort)
    const timeout = setTimeout(() => abort.abort(), 30000)
    try { return await operation(abort.signal) }
    finally { clearTimeout(timeout); requests.delete(abort) }
  }
  async function refresh() {
    if (!currentId || busy || reading || !active) return
    reading = true
    const version = epoch
    try {
      const value = await bounded((signal) => getConnection(currentId!, signal))
      if (active && version === epoch) {
        if (!loaded) populate(value)
        else detail = value
        error = ''
      }
    } catch (cause) { if (active && version === epoch) error = managementError(cause) }
    finally { reading = false; if (active) loading = false }
  }
  async function action(operation: () => Promise<void>) {
    if (busy) return
    busy = true; epoch += 1; error = ''; notice = ''
    try { await operation() }
    catch (cause) { if (active) error = managementError(cause) }
    finally {
      credential = ''
      if (active) { busy = false; onChanged() }
    }
  }
  async function save() {
    const payload = input()
    await action(async () => {
      let value = detail
      if (!currentId || !detail || JSON.stringify(payload) !== JSON.stringify({ display_name: detail.display_name, settings: detail.settings })) {
        value = await bounded((signal) => saveConnection(currentId, payload, signal))
        currentId = value.connection_instance_id
        if (active) populate(value)
      }
      if (credential && transport === 'streamable_http' && auth === 'bearer' && currentId) {
        value = await bounded((signal) => saveCredential(currentId!, credential, signal))
      }
      if (active && value) {
        populate(value)
        notice = value.last_check_error ? '設定を保存しました。接続を確認できなかったため、再試行してください。' : '設定を保存しました。'
      }
    })
  }
  async function check() {
    if (!currentId) return
    await action(async () => {
      const value = await bounded((signal) => checkConnection(currentId!, signal))
      if (active) { detail = value; notice = value.last_check_error ? '接続確認に失敗しました。' : '接続を確認しました。' }
    })
  }
  async function remove() {
    if (!currentId) return
    await action(async () => {
      await bounded((signal) => deleteConnection(currentId!, signal))
      if (active) { onChanged(); onClose() }
    })
  }
  onMount(() => { heading.focus(); void refresh(); timer = setInterval(() => { void refresh() }, 5000) })
  onDestroy(() => { active = false; epoch += 1; clearInterval(timer); for (const request of requests) request.abort(); credential = '' })
</script>

<section aria-labelledby="connection-heading" aria-busy={busy}>
  <header>
    <h2 id="connection-heading" bind:this={heading} tabindex="-1">{currentId ? '外部MCPの詳細・編集' : '外部MCPを追加'}</h2>
    <button type="button" disabled={busy} on:click={onClose}>一覧へ戻る</button>
  </header>
  {#if loading}<p role="status">接続設定を読み込んでいます</p>{/if}
  {#if error}<p role="alert">{error}</p>{/if}
  {#if notice}<p role="status">{notice}</p>{/if}
  {#if busy}<p role="status">処理中です。接続確認には時間がかかる場合があります。</p>{/if}
  {#if detail}
    <div class="connection-status" aria-live="polite">
      <p>利用意思: {detail.desired_enabled ? 'ON' : 'OFF'} / {stateMessage(detail)}</p>
      <p>{confirmationMessage(detail)}</p>
      {#if checkErrorMessage(detail)}<p>前回の確認結果: {checkErrorMessage(detail)}</p>{/if}
      {#if detail.last_success_at}<p>最終成功: <time datetime={detail.last_success_at}>{new Date(detail.last_success_at).toLocaleString('ja-JP')}</time></p>{/if}
    </div>
  {:else}<p>新しい接続はOFFで保存します。接続確認に成功した後、一覧からONにできます。</p>{/if}
  {#if !loading}
    <form on:submit|preventDefault={save}>
      <fieldset disabled={busy}>
        <legend>接続設定</legend>
        <label>表示名<input required maxlength="128" bind:value={displayName} autocomplete="off" /></label>
        <label>接続方式<select aria-label="接続方式" bind:value={transport}><option value="streamable_http">Streamable HTTP</option><option value="stdio">stdio（ローカルプログラム）</option></select></label>
        {#if transport === 'streamable_http'}
          <label>接続先URL<input required type="url" maxlength="4096" bind:value={endpoint} placeholder="https://example.com/mcp" autocomplete="off" spellcheck="false" /></label>
          <label>認証<select aria-label="認証" bind:value={auth}><option value="none">なし</option><option value="bearer">Bearer / APIキー</option></select></label>
          {#if auth === 'bearer'}
            <p>credential: {detail?.credential_set ? '設定済み' : '未設定'}</p>
            <label>{detail?.credential_set ? '新しいAPIキー（更新する場合のみ）' : 'APIキー'}<input type="password" maxlength="16384" bind:value={credential} autocomplete="new-password" spellcheck="false" /></label>
            <small>保存済みの値は表示しません。空欄の場合は更新しません。</small>
          {/if}
        {:else}
          <p>接続確認時に、このBackend環境で指定プログラムを起動します。</p>
          <label>実行ファイル<input required bind:value={command} maxlength="4096" placeholder="例: npx、/usr/bin/python3" autocomplete="off" spellcheck="false" /></label>
          <div class="arguments"><span>引数（1項目ずつ入力）</span>
            {#each args as argument, index}
              <div class="argument"><input aria-label={`引数 ${index + 1}`} bind:value={args[index]} maxlength="8192" autocomplete="off" spellcheck="false" /><button type="button" aria-label={`引数 ${index + 1} を削除`} on:click={() => { args = args.filter((_, i) => i !== index) }}>削除</button></div>
            {/each}
            <button type="button" on:click={() => { args = [...args, ''] }}>引数を追加</button>
          </div>
        {/if}
        <div class="actions"><button type="submit">{currentId ? '設定を保存' : '登録して接続確認'}</button>
          {#if currentId}<button type="button" on:click={check}>接続テスト</button>{/if}</div>
      </fieldset>
    </form>
  {/if}
  {#if detail}
    <section aria-labelledby="tools-heading">
      <h3 id="tools-heading">取得したTool</h3>
      {#if detail.capabilities.counts}
        <p>Tool {detail.capabilities.counts.tools}件 / Resource {detail.capabilities.counts.resources}件 / Prompt {detail.capabilities.counts.prompts}件</p>
        {#if !detail.last_success_at}<p>現在の設定では接続未確認です。</p>{/if}
        <ul>{#each detail.capabilities.tools ?? [] as tool}
          <li><strong>{tool.name}</strong><span class="tool-state">{tool.status === 'active' ? '利用対象' : '未対応'}</span><p class="tool-description">{tool.description}</p></li>
        {/each}</ul>
        {#if detail.capabilities.counts.tools === 0}<p>取得したToolは0件です。</p>{/if}
      {:else}<p>現在の設定で取得したTool一覧はありません。</p>{/if}
      <small>接続をONにすると、対応しているToolが利用対象になります。</small>
    </section>
    <div class="deletion">
      {#if confirmingDelete}
        <p>この接続と保存済みcredentialを削除します。外部サービス側のAPIキーは失効しません。</p>
        <button type="button" disabled={busy} on:click={remove}>接続とcredentialを削除</button>
        <button type="button" disabled={busy} on:click={() => { confirmingDelete = false }}>キャンセル</button>
      {:else}<button type="button" disabled={busy} on:click={() => { confirmingDelete = true }}>接続を削除…</button>{/if}
    </div>
  {/if}
</section>

<style>
  section { color: #e2e8f0; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  h2 { font-size: 1.2rem; } h2:focus { outline: none; }
  p, small { line-height: 1.6; overflow-wrap: anywhere; }
  fieldset { border: 1px solid #65758d; border-radius: 10px; padding: 18px; margin: 20px 0; min-width: 0; }
  label { display: grid; gap: 8px; margin: 16px 0; }
  input, select { box-sizing: border-box; width: 100%; min-width: 0; color: #e2e8f0; background: #172235; border: 1px solid #65758d; border-radius: 6px; padding: 10px; font: inherit; }
  button { color: #e2e8f0; background: #263248; border: 1px solid #65758d; border-radius: 8px; padding: 10px 14px; cursor: pointer; min-height: 44px; }
  :is(button, input, select):focus-visible { outline: 2px solid #b8a5ff; outline-offset: 3px; }
  button:disabled, fieldset:disabled { opacity: .65; }
  .actions, .argument { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
  .argument input { flex: 1; }
  .connection-status { border-left: 3px solid #9383be; padding-left: 16px; }
  [role="alert"] { color: #ffb5b5; }
  ul { list-style: none; padding: 0; } li { padding: 12px 0; border-bottom: 1px solid #354056; overflow-wrap: anywhere; }
  .tool-description { white-space: pre-wrap; } .tool-state { margin-left: 12px; color: #a8b4c7; }
  .deletion { margin-top: 32px; border-top: 1px solid #65758d; padding-top: 20px; }
</style>
