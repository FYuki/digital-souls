<script lang="ts">
  import { onDestroy, onMount } from 'svelte'

  export let conversationId: string
  export let conversationTitle: string | null = null
  export let disabled: boolean
  export let returnFocus: HTMLElement | null = null
  export let onConfirm: () => void
  export let onCancel: () => void

  let dialog: HTMLElement
  let cancelButton: HTMLButtonElement

  const cancelFromOutside = (event: PointerEvent) => {
    if (!disabled && dialog !== undefined && !dialog.contains(event.target as Node)) {
      onCancel()
    }
  }

  const handleKeydown = (event: KeyboardEvent) => {
    if (event.key === 'Escape' && !disabled) {
      event.preventDefault()
      onCancel()
      return
    }
    if (event.key === 'Tab' && dialog !== undefined) {
      const focusable = [...dialog.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
  }

  onMount(() => {
    document.addEventListener('pointerdown', cancelFromOutside)
    cancelButton.focus()
  })
  onDestroy(() => {
    document.removeEventListener('pointerdown', cancelFromOutside)
    returnFocus?.focus()
  })
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="backdrop" role="presentation">
  <section bind:this={dialog} role="dialog" aria-modal="true" aria-labelledby="delete-title">
    <h2 id="delete-title">スレッドを完全に削除しますか</h2>
    <p>対象スレッド: {conversationTitle ?? conversationId}</p>
    <p class="id">ID: {conversationId}</p>
    <p>短期会話履歴は失われ、削除後は復元できません。</p>
    <p>RAG長期記憶は削除されません。</p>
    <p>既存のbackup、snapshot、ファイルシステム上の複製からの消去を保証しません。</p>
    <div class="actions">
      <button bind:this={cancelButton} type="button" on:click={onCancel} disabled={disabled}>キャンセル</button>
      <button type="button" class="danger" on:click={onConfirm} disabled={disabled}>完全に削除</button>
    </div>
  </section>
</div>

<style>
  .backdrop { position: fixed; inset: 0; z-index: 90; display: grid; place-items: center; background: rgba(5, 4, 8, 0.68); }
  section { width: min(480px, calc(100% - 32px)); box-sizing: border-box; padding: 24px; border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 14px; color: #f8f3ff; background: #1b1724; box-shadow: 0 24px 70px rgba(0, 0, 0, 0.42); }
  h2 { margin-top: 0; }
  .id { color: #968fa3; font-size: 0.72rem; overflow-wrap: anywhere; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; }
  button { min-height: 40px; padding: 8px 12px; border: 1px solid #574d63; border-radius: 9px; color: #eee8f3; background: #272130; }
  button:focus-visible { outline: 2px solid #f0a3c1; outline-offset: 2px; }
  .danger { color: white; background: #9d281f; }
</style>
