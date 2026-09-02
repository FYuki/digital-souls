<script lang="ts">
  import { onDestroy, onMount, tick } from 'svelte'

  import HardDeleteDialog from './HardDeleteDialog.svelte'
  import RenameThreadDialog from './RenameThreadDialog.svelte'
  import type { Conversation } from './conversations/types'
  import type { HistoryHeightPercent, PortraitLayout } from './ui-settings/types'
  import {
    collapsedThreads,
    selectCharacterGroups,
    type SidebarController,
    type SidebarState,
  } from './sidebar/controller'

  export let state: SidebarState
  export let controller: SidebarController
  export let selectedCharacter: string
  export let selectedConversationId: string | null
  export let disabled: boolean
  export let onClose: () => void
  export let onSelect: (characterId: string, conversationId: string) => void
  export let onCreated: (characterId: string, conversation: Conversation) => void
  export let onRemoved: (characterId: string, conversationId: string) => void
  export let onRenamed: (characterId: string, conversation: Conversation) => void
  export let onOpenMemory: () => void

  type DialogCandidate = {
    characterId: string
    conversation: Conversation
    returnFocus: HTMLElement | null
  }

  let openMenu: string | null = null
  let menuReturnFocus: HTMLElement | null = null
  let renameCandidate: DialogCandidate | null = null
  let deleteCandidate: DialogCandidate | null = null
  let showingSettings = false
  let addCharacterId = ''

  $: groups = selectCharacterGroups(state)
  $: configuredIds = new Set(
    state.settings?.characters
      .filter((item) => item.visible)
      .map((item) => item.character_id) ?? [],
  )
  $: addCandidates = state.catalog.filter(
    (item) => !configuredIds.has(item.character_id),
  )
  $: sidebarDisabled = disabled
    || state.pending
    || renameCandidate !== null
    || deleteCandidate !== null
  $: if (
    addCharacterId !== ''
    && !addCandidates.some((item) => item.character_id === addCharacterId)
  ) addCharacterId = ''

  const isThreadPinned = (conversation: Conversation): boolean => (
    state.settings?.thread_pins.some((item) => (
      item.character_id === conversation.character_id
      && item.conversation_id === conversation.conversation_id
    )) ?? false
  )

  const closeMenu = async (restoreFocus = true) => {
    openMenu = null
    if (restoreFocus) {
      await tick()
      menuReturnFocus?.focus()
    }
  }

  const toggleMenu = async (key: string, trigger: HTMLElement) => {
    if (openMenu === key) {
      await closeMenu()
      return
    }
    openMenu = key
    menuReturnFocus = trigger
    await tick()
    document.querySelector<HTMLElement>(`[data-menu-for="${key}"] [role="menuitem"]`)?.focus()
  }

  const handleDocumentPointer = (event: PointerEvent) => {
    const target = event.target
    if (
      openMenu !== null
      && (!(target instanceof Element)
        || !target.closest(`[data-thread-menu="${openMenu}"]`))
    ) void closeMenu()
  }

  const handleKeydown = (event: KeyboardEvent) => {
    if (openMenu !== null && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      const items = [...document.querySelectorAll<HTMLElement>(
        `[data-menu-for="${openMenu}"] [role="menuitem"]`,
      )]
      if (items.length === 0) return
      event.preventDefault()
      const index = items.indexOf(document.activeElement as HTMLElement)
      if (event.key === 'Home') items[0].focus()
      else if (event.key === 'End') items[items.length - 1].focus()
      else if (event.key === 'ArrowDown') items[(index + 1) % items.length].focus()
      else items[(index - 1 + items.length) % items.length].focus()
      return
    }
    if (event.key !== 'Escape') return
    if (openMenu !== null) {
      event.preventDefault()
      void closeMenu()
    } else if (showingSettings) {
      event.preventDefault()
      showingSettings = false
    } else if (!showingSettings && renameCandidate === null && deleteCandidate === null) {
      onClose()
    }
  }

  const createThread = async (characterId: string) => {
    if (disabled) return
    const created = await controller.createConversation(characterId)
    if (created !== null) onCreated(characterId, created)
  }

  const openRename = async (characterId: string, conversation: Conversation) => {
    renameCandidate = { characterId, conversation, returnFocus: menuReturnFocus }
    await closeMenu(false)
  }

  const renameThread = async (title: string) => {
    const candidate = renameCandidate
    if (candidate === null) return
    const succeeded = await controller.renameConversation(
      candidate.characterId,
      candidate.conversation.conversation_id,
      title,
    )
    if (!succeeded) return
    const renamed = { ...candidate.conversation, title }
    renameCandidate = null
    onRenamed(candidate.characterId, renamed)
  }

  const archiveThread = async (characterId: string, conversationId: string) => {
    await closeMenu(false)
    if (await controller.archiveConversation(characterId, conversationId)) {
      onRemoved(characterId, conversationId)
    }
  }

  const deleteThread = async () => {
    const candidate = deleteCandidate
    if (candidate === null) return
    if (await controller.hardDeleteConversation(
      candidate.characterId,
      candidate.conversation.conversation_id,
    )) {
      deleteCandidate = null
      onRemoved(candidate.characterId, candidate.conversation.conversation_id)
    }
  }

  const addCharacter = async () => {
    if (addCharacterId === '') return
    if (await controller.addCharacter(addCharacterId)) addCharacterId = ''
  }

  const updatePortraitLayout = (event: Event) => {
    const desktop_portrait_layout = (event.currentTarget as HTMLSelectElement)
      .value as PortraitLayout
    void controller.updatePreferences({ desktop_portrait_layout })
  }

  const updateHistoryHeight = (
    key: 'desktop_history_height_percent' | 'compact_history_height_percent',
    event: Event,
  ) => {
    const value = Number(
      (event.currentTarget as HTMLSelectElement).value,
    ) as HistoryHeightPercent
    void controller.updatePreferences({ [key]: value })
  }

  onMount(() => document.addEventListener('pointerdown', handleDocumentPointer))
  onDestroy(() => document.removeEventListener('pointerdown', handleDocumentPointer))
</script>

<svelte:window on:keydown={handleKeydown} />

<aside class="sidebar" aria-label="スレッド一覧">
  <div class="brand">
    <span class="brand-mark" aria-hidden="true">✦</span>
    <div><p class="brand-name">digital-souls</p><p class="brand-note">character conversations</p></div>
    <button class="icon-button close" type="button" on:click={onClose} aria-label="サイドバーを閉じる">‹</button>
  </div>
  <div class="sidebar-heading">
    <h2>{state.showingArchived ? 'アーカイブ済み' : '会話履歴'}</h2>
    {#if state.pending}<span class="loading" aria-live="polite">更新中</span>{/if}
  </div>
  {#if state.error !== null}<p class="sidebar-error" role="alert">{state.error}</p>{/if}

  <div class="thread-groups">
    {#if !state.initialized && state.pending}
      <p class="empty">読み込んでいます…</p>
    {:else}
      {#each groups as group (group.character.character_id)}
        {@const subset = state.settings === null ? { visible: [], hiddenCount: 0 } : collapsedThreads(group, state.settings)}
        <section class="character-block" aria-labelledby={`character-${group.character.character_id}`}>
          <header class="character-head">
            <button class:active-pin={group.pinned} class="pin-character" type="button" disabled={sidebarDisabled} aria-label={`${group.character.display_name}を${group.pinned ? 'ピン留め解除' : 'ピン留め'}`} aria-pressed={group.pinned} on:click={() => { void controller.setCharacterPinned(group.character.character_id, !group.pinned) }}>★</button>
            <div class="character-label"><strong id={`character-${group.character.character_id}`}>{group.character.display_name}</strong><span>{group.conversations.length}件</span></div>
            {#if !state.showingArchived}
              <button class="icon-button" type="button" disabled={sidebarDisabled} aria-label={`新規スレッド（${group.character.display_name}）`} on:click={() => { void createThread(group.character.character_id) }}>＋</button>
            {:else}<span></span>{/if}
          </header>

          {#if subset.visible.length === 0}
            <p class="empty small">スレッドはありません</p>
          {:else}
            <ul class="thread-list">
              {#each subset.visible as conversation (conversation.conversation_id)}
                {@const pinned = isThreadPinned(conversation)}
                {@const menuKey = `${conversation.character_id}-${conversation.conversation_id}`}
                <li class:selected={!state.showingArchived && selectedCharacter === conversation.character_id && selectedConversationId === conversation.conversation_id} class="thread-row">
                  {#if state.showingArchived}
                    <span class="thread-title archived-title">{#if pinned}<span class="thread-pin" aria-label="ピン留め済み">★</span>{/if}{conversation.title}</span>
                  {:else}
                    <button class="thread-select" type="button" disabled={sidebarDisabled} aria-current={selectedConversationId === conversation.conversation_id ? 'page' : undefined} on:click={() => onSelect(conversation.character_id, conversation.conversation_id)}>
                      {#if pinned}<span class="thread-pin" aria-label="ピン留め済み">★</span>{/if}<span class="thread-title">{conversation.title}</span>
                    </button>
                  {/if}
                  <div class="menu-wrap" data-thread-menu={menuKey}>
                    <button class="thread-menu" type="button" disabled={sidebarDisabled} aria-label={`${conversation.title}のメニュー`} aria-haspopup="menu" aria-expanded={openMenu === menuKey} on:click={(event) => { void toggleMenu(menuKey, event.currentTarget) }}>…</button>
                    {#if openMenu === menuKey}
                      <div class="menu" role="menu" data-menu-for={menuKey}>
                        {#if !state.showingArchived}
                          <button role="menuitem" type="button" on:click={() => { void controller.setThreadPinned(conversation.character_id, conversation.conversation_id, !pinned); void closeMenu(false) }}>{pinned ? 'ピン留め解除' : 'ピン留め'}</button>
                        {/if}
                        <button role="menuitem" type="button" on:click={() => { void openRename(group.character.character_id, conversation) }}>名前を変更</button>
                        {#if state.showingArchived}
                          <button role="menuitem" type="button" on:click={() => { void controller.unarchiveConversation(conversation.character_id, conversation.conversation_id); void closeMenu(false) }}>復元</button>
                        {:else}
                          <button role="menuitem" type="button" on:click={() => { void archiveThread(conversation.character_id, conversation.conversation_id) }}>アーカイブ</button>
                        {/if}
                        <button class="danger" role="menuitem" type="button" on:click={() => { deleteCandidate = { characterId: conversation.character_id, conversation, returnFocus: menuReturnFocus }; void closeMenu(false) }}>削除</button>
                      </div>
                    {/if}
                  </div>
                </li>
              {/each}
            </ul>
          {/if}
          {#if subset.hiddenCount > 0 || group.expanded}
            <button class="show-more" type="button" on:click={() => controller.toggleExpanded(group.character.character_id)}>{group.expanded ? '折りたたむ' : `他${subset.hiddenCount}件を表示`}</button>
          {/if}
          <button class="hide-character" type="button" disabled={sidebarDisabled} on:click={() => { void controller.hideCharacter(group.character.character_id) }}>{group.character.display_name}を一覧から非表示</button>
        </section>
      {/each}
    {/if}
  </div>

  {#if showingSettings}
    <section class="settings-panel" aria-label="設定">
      <div class="settings-head"><h2>設定</h2><button type="button" on:click={() => { showingSettings = false }} aria-label="設定を閉じる">×</button></div>
      <label for="add-character">キャラクター追加</label>
      <select id="add-character" bind:value={addCharacterId} disabled={state.pending}>
        <option value="">選択してください</option>
        {#each addCandidates as character}<option value={character.character_id}>{character.display_name} ({character.character_id})</option>{/each}
      </select>
      <div class="settings-actions">
        <button type="button" disabled={addCharacterId === '' || state.pending} on:click={() => { void addCharacter() }}>追加</button>
        <button type="button" disabled={state.pending} on:click={() => { void controller.rescan() }}>一覧を再読み込み</button>
      </div>
      {#if state.settings !== null}
        <div class="layout-settings">
          <label for="desktop-portrait-layout">PCの立ち絵配置</label>
          <select
            id="desktop-portrait-layout"
            value={state.settings.desktop_portrait_layout}
            disabled={state.pending}
            on:change={updatePortraitLayout}
          >
            <option value="right">会話履歴の右側</option>
            <option value="background">会話履歴の背面</option>
          </select>
          <label for="desktop-history-height">PC・履歴背面の表示範囲</label>
          <select
            id="desktop-history-height"
            value={state.settings.desktop_history_height_percent}
            disabled={state.pending}
            on:change={(event) => updateHistoryHeight('desktop_history_height_percent', event)}
          >
            <option value="50">下部50%</option>
            <option value="75">下部75%</option>
            <option value="100">下部100%</option>
          </select>
          <label for="compact-history-height">タブレット・モバイルの表示範囲</label>
          <select
            id="compact-history-height"
            value={state.settings.compact_history_height_percent}
            disabled={state.pending}
            on:change={(event) => updateHistoryHeight('compact_history_height_percent', event)}
          >
            <option value="50">下部50%</option>
            <option value="75">下部75%</option>
            <option value="100">下部100%</option>
          </select>
        </div>
      {/if}
    </section>
  {/if}

  <nav class="sidebar-bottom" aria-label="サイドバーメニュー">
    <button class:active={state.showingArchived} class="side-action" type="button" disabled={sidebarDisabled} on:click={() => state.showingArchived ? controller.showActive() : void controller.showArchived()}><span aria-hidden="true">▣</span>{state.showingArchived ? '会話履歴に戻る' : 'アーカイブ済み'}</button>
    <button class="side-action" type="button" on:click={() => { showingSettings = !showingSettings }}><span aria-hidden="true">⚙</span>設定</button>
    <button class="side-action" type="button" on:click={onOpenMemory}><span aria-hidden="true">◇</span>記憶管理</button>
  </nav>
</aside>

{#if renameCandidate !== null}
  <RenameThreadDialog currentTitle={renameCandidate.conversation.title} disabled={state.pending} returnFocus={renameCandidate.returnFocus} onConfirm={(title) => { void renameThread(title) }} onCancel={() => { renameCandidate = null }} />
{/if}
{#if deleteCandidate !== null}
  <HardDeleteDialog conversationId={deleteCandidate.conversation.conversation_id} conversationTitle={deleteCandidate.conversation.title} disabled={state.pending} returnFocus={deleteCandidate.returnFocus} onConfirm={() => { void deleteThread() }} onCancel={() => { deleteCandidate = null }} />
{/if}

<style>
  .sidebar { position: relative; z-index: 50; display: flex; width: 292px; min-height: 0; flex: 0 0 292px; flex-direction: column; border-right: 1px solid rgba(255, 255, 255, 0.09); color: #f8f3ff; background: rgba(17, 14, 24, 0.98); box-shadow: 14px 0 40px rgba(0, 0, 0, 0.18); }
  .brand { display: grid; grid-template-columns: 36px minmax(0, 1fr) 44px; align-items: center; gap: 10px; padding: 17px 8px 13px 18px; }
  .brand-mark { display: grid; width: 34px; height: 34px; place-items: center; border: 1px solid rgba(240, 163, 193, 0.32); border-radius: 11px; color: #ffd5e5; background: linear-gradient(145deg, rgba(240, 163, 193, 0.2), rgba(156, 130, 255, 0.16)); }
  .brand p { margin: 0; }
  .brand-name { font-size: 0.92rem; font-weight: 750; }
  .brand-note { margin-top: 2px !important; color: #968fa3; font-size: 0.64rem; }
  button, select { font: inherit; }
  button { color: inherit; cursor: pointer; }
  button:disabled { cursor: not-allowed; opacity: 0.48; }
  button:focus-visible, select:focus-visible { outline: 2px solid #f0a3c1; outline-offset: 2px; }
  .icon-button, .thread-menu, .pin-character { display: inline-grid; min-width: 44px; min-height: 44px; place-items: center; border: 0; border-radius: 9px; background: transparent; }
  .icon-button:hover, .thread-menu:hover, .pin-character:hover { background: rgba(255, 255, 255, 0.075); }
  .close { font-size: 1.35rem; }
  .sidebar-heading { display: flex; align-items: center; justify-content: space-between; padding: 5px 14px 10px 18px; }
  .sidebar-heading h2 { margin: 0; color: #a9a1b5; font-size: 0.76rem; }
  .loading { color: #8d8598; font-size: 0.65rem; }
  .sidebar-error { margin: 0 12px 8px; padding: 8px; border-radius: 8px; color: #ffd1d1; background: rgba(177, 50, 50, 0.22); font-size: 0.72rem; }
  .thread-groups { min-height: 0; flex: 1; overflow-y: auto; padding: 0 10px 18px; scrollbar-width: thin; scrollbar-color: #443b50 transparent; }
  .character-block { margin: 0 0 13px; }
  .character-head { display: grid; grid-template-columns: 44px minmax(0, 1fr) 44px; align-items: center; min-height: 44px; gap: 2px; padding: 2px 0 3px; }
  .character-label { min-width: 0; }
  .character-label strong { display: block; overflow: hidden; color: #cbc3d4; font-size: 0.79rem; text-overflow: ellipsis; white-space: nowrap; }
  .character-label span { color: #766f81; font-size: 0.65rem; }
  .pin-character { color: #6e6875; }
  .pin-character.active-pin, .thread-pin { color: #f5bc77; }
  .thread-list { display: grid; gap: 2px; margin: 0; padding: 0; list-style: none; }
  .thread-row { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) 44px; align-items: center; min-height: 44px; border-radius: 10px; }
  .thread-row:hover { background: rgba(255, 255, 255, 0.045); }
  .thread-row.selected { background: linear-gradient(90deg, rgba(240, 163, 193, 0.14), rgba(156, 130, 255, 0.1)); box-shadow: inset 2px 0 #f0a3c1; }
  .thread-select { display: flex; min-width: 0; min-height: 44px; align-items: center; gap: 7px; padding: 0 6px 0 10px; border: 0; color: #d7d0df; background: transparent; text-align: left; }
  .thread-title { overflow: hidden; min-width: 0; flex: 1; font-size: 0.77rem; text-overflow: ellipsis; white-space: nowrap; }
  .archived-title { display: flex; align-items: center; gap: 7px; padding-left: 10px; color: #b9b1c2; }
  .thread-pin { font-size: 0.65rem; }
  .thread-menu { color: #85808d; }
  .menu-wrap { position: relative; }
  .menu { position: absolute; z-index: 70; top: 31px; right: 0; display: grid; width: 152px; padding: 5px; border: 1px solid rgba(255, 255, 255, 0.11); border-radius: 10px; background: #282230; box-shadow: 0 14px 35px rgba(0, 0, 0, 0.4); }
  .menu button { min-height: 44px; padding: 7px 10px; border: 0; border-radius: 7px; background: transparent; text-align: left; }
  .menu button:hover, .menu button:focus-visible { background: rgba(255, 255, 255, 0.08); }
  .menu .danger { color: #ffb8b8; }
  .show-more, .hide-character { width: 100%; min-height: 44px; padding: 7px 9px; border: 0; border-radius: 8px; color: #928a9d; background: transparent; font-size: 0.68rem; text-align: left; }
  .show-more:hover, .hide-character:hover { color: #ddd4e6; background: rgba(255, 255, 255, 0.045); }
  .hide-character { margin-top: 2px; color: #756d7d; }
  .empty { margin: 12px 8px; color: #8b8394; font-size: 0.76rem; }
  .empty.small { margin: 5px 10px; font-size: 0.68rem; }
  .settings-panel { max-height: 58dvh; overflow-y: auto; padding: 13px; border-top: 1px solid rgba(255, 255, 255, 0.09); background: #18131f; }
  .settings-head { display: flex; align-items: center; justify-content: space-between; }
  .settings-head h2 { margin: 0; font-size: 0.85rem; }
  .settings-head button { min-width: 44px; min-height: 44px; border: 0; color: #aaa2b3; background: transparent; }
  .settings-panel label { display: block; margin: 10px 0 5px; color: #aaa2b3; font-size: 0.7rem; }
  .settings-panel select { width: 100%; min-height: 44px; padding: 6px 8px; border: 1px solid #4c4358; border-radius: 8px; color: #eee8f3; background: #100d17; }
  .settings-actions { display: flex; gap: 6px; margin-top: 8px; }
  .settings-actions button { min-height: 44px; padding: 6px 9px; border: 1px solid #4c4358; border-radius: 8px; background: #292231; font-size: 0.7rem; }
  .layout-settings { display: grid; gap: 5px; margin-top: 14px; padding-top: 12px; border-top: 1px solid rgba(255, 255, 255, 0.09); }
  .layout-settings label { margin-top: 6px; }
  .sidebar-bottom { display: grid; gap: 3px; padding: 10px; border-top: 1px solid rgba(255, 255, 255, 0.09); background: rgba(15, 12, 22, 0.98); }
  .side-action { display: flex; min-height: 44px; align-items: center; gap: 10px; padding: 0 11px; border: 0; border-radius: 10px; color: #bbb3c5; background: transparent; font-size: 0.78rem; }
  .side-action:hover, .side-action.active { color: #fff; background: rgba(255, 255, 255, 0.06); }
  .side-action span { width: 18px; color: #a99dc3; text-align: center; }
</style>
