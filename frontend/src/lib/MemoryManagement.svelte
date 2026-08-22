<script lang="ts">
  import { onMount } from 'svelte'

  import {
    MemoryCorrectionRejected,
    correctPersonaMemory,
    correctTemporaryRecord,
    hardDeletePersonaMemory,
    hardDeleteTemporaryRecord,
    listPersonaMemories,
    listTemporaryRecords,
    type PersonaMemory,
    type TemporaryRecord,
  } from './memory/client'

  export let character: string
  export let onClose: () => void

  let personaMemories: PersonaMemory[] = []
  let temporaryRecords: TemporaryRecord[] = []
  let selectedMemory: PersonaMemory | null = null
  let selectedRecord: TemporaryRecord | null = null
  let deletePersonaCandidate: PersonaMemory | null = null
  let deleteTemporaryCandidate: TemporaryRecord | null = null
  let reasonCode: string | null = null
  let correctionText = ''
  let temporaryRecordType = ''
  let temporaryStructuredValue = ''
  let temporaryEffectiveAt = ''
  let loading = true
  let error: string | null = null

  onMount(async () => {
    try {
      const [persona, recipes, agriculture] = await Promise.all([
        listPersonaMemories(character),
        listTemporaryRecords(character, 'temporary:recipe'),
        listTemporaryRecords(character, 'temporary:agriculture'),
      ])
      personaMemories = persona
      temporaryRecords = [...recipes, ...agriculture]
    } catch {
      error = '記憶の取得に失敗しました。'
    } finally {
      loading = false
    }
  })

  const saveCorrection = async () => {
    if (selectedMemory === null) return
    reasonCode = null
    try {
      const parsed: unknown = JSON.parse(correctionText)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        throw new Error('訂正値はJSON objectである必要があります。')
      }
      const corrected = await correctPersonaMemory(
        character,
        selectedMemory,
        parsed as Record<string, unknown>,
      )
      personaMemories = personaMemories.map((memory) => (
        memory.id === corrected.id ? corrected : memory
      ))
      selectedMemory = corrected
    } catch (caught) {
      if (caught instanceof MemoryCorrectionRejected) {
        reasonCode = caught.reasonCode
        return
      }
      error = '訂正に失敗しました。'
    }
  }

  const confirmDelete = async () => {
    if (deletePersonaCandidate === null) return
    const memoryId = deletePersonaCandidate.id
    try {
      await hardDeletePersonaMemory(character, memoryId)
      personaMemories = personaMemories.filter((memory) => memory.id !== memoryId)
      if (selectedMemory?.id === memoryId) selectedMemory = null
      deletePersonaCandidate = null
      reasonCode = null
    } catch {
      error = '削除に失敗しました。'
    }
  }

  const saveTemporaryCorrection = async () => {
    if (selectedRecord === null) return
    try {
      const corrected = await correctTemporaryRecord(
        character,
        selectedRecord,
        {
          record_type: temporaryRecordType,
          structured_value: temporaryStructuredValue,
          effective_at: temporaryEffectiveAt,
        },
      )
      temporaryRecords = temporaryRecords.map((record) => (
        record.id === corrected.id ? corrected : record
      ))
      selectedRecord = corrected
      temporaryRecordType = corrected.record_type
      temporaryStructuredValue = corrected.structured_value
      temporaryEffectiveAt = corrected.effective_at
    } catch {
      error = '訂正に失敗しました。'
    }
  }

  const confirmTemporaryDelete = async () => {
    if (deleteTemporaryCandidate === null) return
    const record = deleteTemporaryCandidate
    try {
      await hardDeleteTemporaryRecord(character, record)
      temporaryRecords = temporaryRecords.filter((item) => item.id !== record.id)
      if (selectedRecord?.id === record.id) selectedRecord = null
      deleteTemporaryCandidate = null
    } catch {
      error = '削除に失敗しました。'
    }
  }
</script>

<section class="memory-management" aria-label="記憶管理">
  <header>
    <div>
      <p class="eyebrow">memory management</p>
      <h1>記憶管理</h1>
    </div>
    <button type="button" on:click={onClose}>チャットに戻る</button>
  </header>

  {#if loading}
    <p>読み込み中</p>
  {:else}
    {#if error !== null}<p role="alert">{error}</p>{/if}
    <section aria-labelledby="persona-heading">
      <h2 id="persona-heading">人格記憶</h2>
      {#each personaMemories as memory (memory.id)}
        <article>
          <div class="record-heading"><strong>{memory.memory_type}</strong><span>{memory.status}</span></div>
          <p>{memory.normalized_text}</p>
          <time datetime={memory.effective_at}>{memory.effective_at}</time>
          {#if memory.index_pending}<p class="pending">index反映待ち</p>{/if}
          <div class="actions">
            <button type="button" aria-label={`詳細・訂正 ${memory.id}`} on:click={() => { selectedMemory = memory; selectedRecord = null; correctionText = JSON.stringify(memory.structured_value ?? { polarity: 'LIKE', object: memory.normalized_text }); reasonCode = null }}>詳細・訂正</button>
            <button type="button" aria-label={`削除 ${memory.id}`} on:click={() => { deletePersonaCandidate = memory }}>削除</button>
          </div>
        </article>
      {/each}
    </section>

    <section aria-labelledby="temporary-heading">
      <h2 id="temporary-heading">暫定記録</h2>
      {#each temporaryRecords as record (record.id)}
        <article>
          <div class="record-heading"><strong>{record.record_type}</strong></div>
          <p>{record.structured_value}</p>
          <time datetime={record.effective_at}>{record.effective_at}</time>
          <time datetime={record.updated_at}>{record.updated_at}</time>
          <div class="actions">
            <button type="button" aria-label={`詳細・訂正 ${record.id}`} on:click={() => { selectedRecord = record; selectedMemory = null; temporaryRecordType = record.record_type; temporaryStructuredValue = record.structured_value; temporaryEffectiveAt = record.effective_at }}>詳細・訂正</button>
            <button type="button" aria-label={`削除 ${record.id}`} on:click={() => { deleteTemporaryCandidate = record }}>削除</button>
          </div>
        </article>
      {/each}
    </section>
  {/if}
</section>

{#if selectedMemory !== null}
  <section class="editor" aria-label="人格記憶の訂正">
    <h2>人格記憶を訂正</h2>
    <label>正規化内容<textarea bind:value={correctionText}></textarea></label>
    {#if reasonCode !== null}
      <p role="alert">{reasonCode}</p>
      <button type="button" on:click={() => { reasonCode = null }}>再編集</button>
    {/if}
    <button type="button" on:click={saveCorrection}>訂正を保存</button>
  </section>
{/if}

{#if selectedRecord !== null}
  <section class="editor" aria-label="暫定記録の訂正">
    <h2>暫定記録を訂正</h2>
    <label>種別<input bind:value={temporaryRecordType} /></label>
    <label>構造化値<textarea bind:value={temporaryStructuredValue}></textarea></label>
    <label>有効日時<input bind:value={temporaryEffectiveAt} /></label>
    <button type="button" on:click={saveTemporaryCorrection}>訂正を保存</button>
  </section>
{/if}

{#if deletePersonaCandidate !== null}
  <div class="backdrop" role="presentation">
    <section class="dialog" role="dialog" aria-modal="true" aria-labelledby="memory-delete-title">
      <h2 id="memory-delete-title">記憶を完全に削除しますか</h2>
      <p>削除後は復元できません。</p>
      <div class="actions">
        <button type="button" on:click={() => { deletePersonaCandidate = null }}>キャンセル</button>
        <button type="button" class="danger" on:click={confirmDelete}>完全に削除</button>
      </div>
    </section>
  </div>
{/if}

{#if deleteTemporaryCandidate !== null}
  <div class="backdrop" role="presentation">
    <section class="dialog" role="dialog" aria-modal="true" aria-labelledby="record-delete-title">
      <h2 id="record-delete-title">暫定記録を完全に削除しますか</h2>
      <p>削除後は復元できません。</p>
      <div class="actions">
        <button type="button" on:click={() => { deleteTemporaryCandidate = null }}>キャンセル</button>
        <button type="button" class="danger" on:click={confirmTemporaryDelete}>完全に削除</button>
      </div>
    </section>
  </div>
{/if}

<style>
  .memory-management { width: min(880px, 100%); min-height: calc(100vh - 48px); box-sizing: border-box; padding: 24px; border: 1px solid rgba(144, 67, 47, .2); border-radius: 8px; background: #fffdfa; }
  header, .record-heading, .actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  header { border-bottom: 1px solid rgba(144, 67, 47, .16); }
  h1, h2 { color: #4a2822; }
  .eyebrow { margin: 0; color: #9f4933; font-size: .78rem; font-weight: 700; text-transform: uppercase; }
  article { margin: 12px 0; padding: 16px; border: 1px solid rgba(144, 67, 47, .16); border-radius: 8px; }
  time { display: block; color: #69524d; }
  .pending { color: #9f4933; font-weight: 700; }
  .editor { position: fixed; right: 24px; bottom: 24px; z-index: 5; width: min(420px, calc(100% - 48px)); box-sizing: border-box; padding: 20px; border: 1px solid #bc806d; border-radius: 8px; background: white; box-shadow: 0 12px 32px rgba(69, 39, 33, .2); }
  input, textarea { display: block; width: 100%; box-sizing: border-box; }
  textarea { min-height: 80px; }
  .backdrop { position: fixed; inset: 0; z-index: 10; display: grid; place-items: center; background: rgba(40, 20, 16, .45); }
  .dialog { width: min(480px, calc(100% - 32px)); box-sizing: border-box; padding: 24px; border-radius: 8px; background: white; }
  .danger { color: white; background: #9d281f; }
</style>
