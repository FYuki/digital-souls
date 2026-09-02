<script lang="ts">
  import { onDestroy, onMount } from 'svelte'

  export let currentTitle: string
  export let disabled: boolean
  export let returnFocus: HTMLElement | null = null
  export let onConfirm: (title: string) => void
  export let onCancel: () => void

  let dialog: HTMLElement
  let titleInput: HTMLInputElement
  let title = currentTitle

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
      const focusable = [...dialog.querySelectorAll<HTMLElement>(
        'input:not(:disabled), button:not(:disabled)',
      )]
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

  const submit = () => {
    const normalized = title.trim()
    if (disabled || normalized.length === 0 || normalized.length > 40) return
    onConfirm(normalized)
  }

  onMount(() => {
    document.addEventListener('pointerdown', cancelFromOutside)
    titleInput.focus()
    titleInput.select()
  })
  onDestroy(() => {
    document.removeEventListener('pointerdown', cancelFromOutside)
    returnFocus?.focus()
  })
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="backdrop" role="presentation">
  <section bind:this={dialog} role="dialog" aria-modal="true" aria-labelledby="rename-title">
    <h2 id="rename-title">スレッド名を変更</h2>
    <form on:submit|preventDefault={submit}>
      <label for="thread-title">スレッド名</label>
      <input
        bind:this={titleInput}
        id="thread-title"
        bind:value={title}
        maxlength="40"
        disabled={disabled}
      />
      <p class="count">{title.length} / 40</p>
      <div class="actions">
        <button type="button" on:click={onCancel} disabled={disabled}>キャンセル</button>
        <button type="submit" class="primary" disabled={disabled || title.trim().length === 0}>
          保存
        </button>
      </div>
    </form>
  </section>
</div>

<style>
  .backdrop { position: fixed; inset: 0; z-index: 90; display: grid; place-items: center; background: rgba(5, 4, 8, 0.68); }
  section { width: min(420px, calc(100% - 32px)); box-sizing: border-box; padding: 24px; border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 14px; color: #f8f3ff; background: #1b1724; box-shadow: 0 24px 70px rgba(0, 0, 0, 0.42); }
  h2 { margin: 0 0 18px; font-size: 1.1rem; }
  form, label { display: grid; gap: 8px; }
  label { color: #c8bfd1; font-size: 0.78rem; font-weight: 700; }
  input { min-height: 42px; box-sizing: border-box; padding: 8px 11px; border: 1px solid #574d63; border-radius: 9px; color: #fff; background: #100d17; }
  input:focus-visible { outline: 2px solid #f0a3c1; outline-offset: 2px; }
  .count { margin: 0; color: #8f8699; font-size: 0.7rem; text-align: right; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
  button { min-height: 40px; padding: 8px 13px; border: 1px solid #574d63; border-radius: 9px; color: #eee8f3; background: #272130; }
  .primary { border-color: #b86d89; background: #9d496b; }
</style>
