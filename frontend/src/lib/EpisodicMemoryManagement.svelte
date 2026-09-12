<script lang="ts">
  import { onMount, tick } from 'svelte'
  import MemoryTimeFields from './memory/MemoryTimeFields.svelte'
  import {
    correctFact, deleteFact, listEpisodicMemories, emptyTimeParts, EpisodicRequestError,
    type EpisodicMemory, type FiveW, type MemoryTime, type Person,
  } from './memory/episodic-client'

  export let character: string
  let records: EpisodicMemory[] = []
  let loading = true
  let busy = false
  let error: string | null = null
  let selected: EpisodicMemory | null = null
  let deleting: EpisodicMemory | null = null
  let editKey = ''
  let deleteKey = ''
  let sentBody = ''
  let draft: FiveW
  let place = ''
  let reason = ''
  let object = ''
  let whenEnabled = false
  let time: MemoryTime
  let rangeKind: MemoryTime['range_kind'] = 'POINT'
  let end = emptyTimeParts()
  let editor: HTMLElement
  let deletePanel: HTMLElement
  let trigger: HTMLButtonElement | null = null
  const roles: { value: Person['role']; label: string }[] = [
    { value: 'ACTOR', label: '行為者' }, { value: 'PARTICIPANT', label: '参加者' },
    { value: 'SPEAKER', label: '話し手' }, { value: 'LISTENER', label: '聞き手' }, { value: 'TOPIC', label: '話題の人物' },
  ]
  const states = { ACTIVE: '有効', INACTIVE: '利用停止', DELETED: '削除済み' }

  async function load() {
    loading = true
    error = null
    try { records = await listEpisodicMemories(character) }
    catch { error = '経験と取得情報の読み込みに失敗しました。' }
    finally { loading = false }
  }
  onMount(() => { void load() })
  function showError(caught: unknown) {
    error = caught instanceof EpisodicRequestError
      ? caught.message + (caught.reason ? `（${caught.reason}）` : '') : '通信に失敗しました。再試行できます。'
  }
  async function edit(record: EpisodicMemory, button: HTMLButtonElement) {
    trigger = button
    selected = record
    deleting = null
    draft = structuredClone(record.five_w ?? {
      who: [], what: { predicate: '', object: null, polarity: 'UNKNOWN', actuality: 'UNKNOWN' },
      when: null, where: null, why: null, context: 'UNKNOWN',
    })
    object = draft.what.object ?? ''
    place = draft.where?.name ?? ''
    reason = draft.why ?? ''
    whenEnabled = draft.when !== null
    time = structuredClone(draft.when ?? {
      parts: emptyTimeParts(), end: null, range_kind: 'POINT',
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', reference_at: new Date().toISOString(),
    })
    rangeKind = time.range_kind
    end = structuredClone(time.end ?? emptyTimeParts())
    editKey = crypto.randomUUID()
    sentBody = ''
    error = null
    await tick()
    editor.querySelector<HTMLInputElement>('input')?.focus()
  }
  async function cancel() {
    selected = null
    deleting = null
    error = null
    await tick()
    trigger?.focus()
    trigger = null
  }
  async function save() {
    if (!selected || busy) return
    const value: FiveW = {
      ...draft,
      who: draft.who.map(person => ({
        ...person, name: person.name.trim(),
        entity_id: selected?.five_w?.who.some(old => old.entity_id === person.entity_id && old.name === person.name.trim())
          ? person.entity_id : null,
      })),
      what: { ...draft.what, predicate: draft.what.predicate.trim(), object: object.trim() || null },
      where: place.trim() ? { name: place.trim(), entity_id: selected.five_w?.where?.name === place.trim()
        ? selected.five_w.where.entity_id : null } : null,
      why: reason.trim() || null,
      when: whenEnabled ? { ...time, timezone: time.timezone.trim(), range_kind: rangeKind,
        end: rangeKind === 'POINT' ? null : end } : null,
    }
    const body = JSON.stringify(value)
    if (sentBody && body !== sentBody) editKey = crypto.randomUUID()
    sentBody = body
    busy = true
    error = null
    try {
      await correctFact(character, selected, value, editKey)
      selected = null
      records = []
      await load()
    } catch (caught) { showError(caught) }
    finally { busy = false }
  }
  async function askDelete(record: EpisodicMemory, button: HTMLButtonElement) {
    selected = null
    deleting = record
    trigger = button
    deleteKey = crypto.randomUUID()
    error = null
    await tick()
    deletePanel.querySelector<HTMLButtonElement>('button')?.focus()
  }
  async function confirmDelete() {
    if (!deleting || busy) return
    busy = true
    error = null
    try {
      await deleteFact(character, deleting, deleteKey)
      deleting = null
      records = []
      await load()
    } catch (caught) { showError(caught) }
    finally { busy = false }
  }
  async function reload() {
    selected = null
    deleting = null
    records = []
    await load()
  }
</script>

<section aria-label="経験と取得情報" class="episodic">
  <div class="heading">
    <div><h2>経験と取得情報</h2><p>話を聞いた経験（Episode）と、その話題の情報（Fact）を確認できます。</p></div>
    <button type="button" disabled={busy || loading} on:click={reload}>再読み込み</button>
  </div>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if loading}<p role="status">経験と取得情報を読み込み中</p>
  {:else if records.length === 0 && !error}<p>保存された経験・取得情報はありません。</p>
  {:else}
    {#each records as record (record.id)}
      <article id={`episodic-${record.id}`} aria-label={`${record.kind} ${record.id}`}>
        <div class="heading"><h3>{record.kind === 'EPISODE' ? '経験' : '取得情報'} <small>{record.kind}</small></h3><span>{states[record.status]}・第{record.content_version}版</span></div>
        {#if record.five_w}
          <p class="content">{record.normalized_text}</p>
          <p>文脈：{({ REPORTED: '申告された内容', HYPOTHETICAL: '仮定', FICTIONAL: '創作', UNKNOWN: '不明' })[record.five_w.context]}</p>
        {:else}<p>本文は利用できません。</p>{/if}
        <p class="metadata">ID: {record.id}</p>
        {#if record.kind === 'EPISODE' && record.experienced_when}
          <p>経験日時：{record.experienced_when.parts.year ?? '不明'}年{record.experienced_when.parts.month ?? '不明'}月{record.experienced_when.parts.day ?? '不明'}日（{record.experienced_when.timezone}）</p>
        {/if}
        {#if record.representative_id && record.representative_id !== record.id}
          <p>同じ情報の代表：<a href={`#episodic-${record.representative_id}`}>{record.representative_id}</a></p>
        {/if}
        <details>
          <summary>出典・版・参照を確認</summary>
          <p class="metadata">所有キャラクター：{record.character_id} / 会話：{record.conversation_id ?? '会話外'}</p>
          {#each record.versions as version}
            <h4>第{version.content_version}版 {version.content_erased ? '（本文削除済み）' : ''}</h4>
            <ul>{#each version.sources as source}
              <li class="metadata">{source.role === 'manual' ? '管理画面での変更' : source.role}：{source.source_id} / 出典版 {source.revision} / 範囲 {source.start}–{source.end}<br />{source.stated_at}</li>
            {/each}</ul>
          {/each}
          {#each record.references as link}
            <p class="metadata">経験 <a href={`#episodic-${link.episode_id}`}>{link.episode_id}</a>（第{link.episode_version}版）
              → 取得情報 <a href={`#episodic-${link.fact_id}`}>{link.fact_id}</a>（第{link.fact_version}版）：{link.valid ? '有効' : '失効'}</p>
          {/each}
          {#each record.merges as merge}
            <p class="metadata">同一情報の統合：<a href={`#episodic-${merge.source_fact_id}`}>{merge.source_fact_id}</a>（第{merge.source_version}版）
              → <a href={`#episodic-${merge.target_fact_id}`}>{merge.target_fact_id}</a>（第{merge.target_version}版）：{merge.valid ? '有効' : '失効'} / {merge.policy}</p>
            <ul>{#each merge.evidence as source}
              <li class="metadata">判定の出典：{source.source_id} / 版 {source.revision} / 範囲 {source.start}–{source.end}</li>
            {/each}</ul>
          {/each}
        </details>
        {#if record.kind === 'FACT' && record.status !== 'DELETED'}
          <div class="actions">
            <button type="button" disabled={busy} aria-label={`Factを訂正 ${record.id}`} on:click={(event) => edit(record, event.currentTarget)}>訂正</button>
            <button type="button" disabled={busy} aria-label={`Factを削除 ${record.id}`} on:click={(event) => askDelete(record, event.currentTarget)}>削除</button>
          </div>
        {/if}
      </article>
    {/each}
  {/if}

  {#if selected}
    <form bind:this={editor} aria-label="取得情報の訂正" on:submit|preventDefault={save}>
      <h3>取得情報を訂正</h3>
      <p>第{selected.content_version}版を編集します。不明な情報は空欄のままにできます。</p>
      {#if !selected.five_w}<p>利用停止中の取得情報です。確認できる内容を入力すると、新しい版として保存します。</p>{/if}
      <fieldset disabled={busy}>
        <legend>人物と役割</legend>
        {#each draft.who as person, index}
          <div class="person">
            <label>人物名<input aria-label={`人物名 ${index + 1}`} required maxlength="240" bind:value={person.name} /></label>
            <label>役割<select aria-label={`役割 ${index + 1}`} bind:value={person.role}>
              {#each roles as role}<option value={role.value}>{role.label}</option>{/each}
            </select></label>
            <button type="button" on:click={() => { draft = { ...draft, who: draft.who.filter((_, i) => i !== index) } }}>人物を外す</button>
          </div>
        {/each}
        <button type="button" disabled={draft.who.length >= 16} on:click={() => { draft = { ...draft, who: [...draft.who, { name: '', role: 'ACTOR', entity_id: null }] } }}>人物を追加</button>
      </fieldset>
      <fieldset disabled={busy}>
        <legend>出来事</legend>
        <label>行為・出来事（必須）<input required maxlength="240" bind:value={draft.what.predicate} /></label>
        <label>対象<input maxlength="240" bind:value={object} /></label>
        <label>肯定・否定<select bind:value={draft.what.polarity}><option value="UNKNOWN">不明</option><option value="AFFIRMED">肯定</option><option value="NEGATED">否定</option></select></label>
        <label>実現の状態<select bind:value={draft.what.actuality}><option value="UNKNOWN">不明</option><option value="OCCURRED">起きた</option><option value="PLANNED">予定</option><option value="CONDITIONAL">条件付き</option></select></label>
        <label>文脈<select bind:value={draft.context}><option value="UNKNOWN">不明</option><option value="REPORTED">申告された内容</option><option value="HYPOTHETICAL">仮定</option><option value="FICTIONAL">創作</option></select></label>
        <label>場所<input maxlength="240" bind:value={place} /></label>
        <label>明示された理由<input maxlength="240" bind:value={reason} /></label>
      </fieldset>
      <fieldset disabled={busy}>
        <legend>話題の出来事の日時</legend>
        <label class="checkbox"><input type="checkbox" bind:checked={whenEnabled} />日時情報を設定する</label>
        {#if whenEnabled}
          <MemoryTimeFields label="発生日時" bind:value={time.parts} />
          <label>日時の扱い<select bind:value={rangeKind}><option value="POINT">指定した精度の日時</option><option value="UNCERTAINTY">この範囲内のいずれか</option><option value="DURATION">この期間に継続</option></select></label>
          {#if rangeKind !== 'POINT'}<MemoryTimeFields label="終了日時" bind:value={end} />{/if}
          <label>タイムゾーン<input required bind:value={time.timezone} /></label>
        {/if}
      </fieldset>
      <div class="actions"><button type="button" disabled={busy} on:click={cancel}>キャンセル</button><button disabled={busy} type="submit">Factの訂正を保存</button></div>
    </form>
  {/if}
  {#if deleting}
    <section bind:this={deletePanel} aria-label="取得情報の削除確認" class="confirmation">
      <h3>この取得情報を削除しますか</h3>
      <p>本文と同じ情報に依存する記憶は利用できなくなります。経験の識別情報は残ります。</p>
      <p class="metadata">{deleting.id}・第{deleting.content_version}版</p>
      <div class="actions"><button type="button" disabled={busy} on:click={cancel}>キャンセル</button><button type="button" disabled={busy} on:click={confirmDelete}>Factを削除する</button></div>
    </section>
  {/if}
</section>

<style>
  .episodic { color: #f8f3ff; margin: 24px 0; }
  .heading, .actions { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
  h2, h3 { margin: 8px 0; }
  small, .metadata { color: #c4b6da; }
  .metadata, .content { overflow-wrap: anywhere; }
  article, form, .confirmation { padding: 16px; margin: 14px 0; border: 1px solid #574d63; border-radius: 12px; background: #1b1724; }
  form, .confirmation { border-color: #c4b6da; }
  fieldset { min-width: 0; margin: 12px 0; border: 1px solid #574d63; border-radius: 8px; }
  label { display: grid; gap: 5px; margin: 10px 0; }
  .checkbox { display: flex; align-items: center; }
  .checkbox input { width: auto; }
  .person { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 8px; }
  .person button { grid-column: 1 / -1; }
  button, input, select { min-height: 44px; box-sizing: border-box; padding: 8px; border: 1px solid #574d63; border-radius: 8px; background: #100d17; color: #fff; }
  input, select { width: 100%; }
  button { cursor: pointer; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible { outline: 2px solid #f0a3c1; outline-offset: 2px; }
  summary { cursor: pointer; padding: 12px 0; }
  a { color: #e3b5ff; overflow-wrap: anywhere; }
  .actions { margin-top: 12px; justify-content: flex-end; }
</style>
