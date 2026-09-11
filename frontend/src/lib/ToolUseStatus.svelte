<script lang="ts">
  import { onDestroy, onMount } from 'svelte'
  import ActionConfirmation from './ActionConfirmation.svelte'

  export let character: string
  export let conversationId: string
  export let onStop: () => Promise<void> = async () => undefined
  export let onContinue: (requestId: string) => Promise<void | 'ended'> = async () => undefined

  let state = 'disabled'
  let sources: { label: string; source_id: string }[] = []
  let confirmationId: string | null = null
  let ended = false
  let stopping = false
  let error = ''
  let timer: ReturnType<typeof setTimeout> | undefined
  const controller = new AbortController()

  async function refresh() {
    try {
      const response = await fetch(`/api/tool-use/status/${encodeURIComponent(character)}/${encodeURIComponent(conversationId)}`, {
        signal: controller.signal,
        cache: 'no-store',
      })
      if (!response.ok) return
      const value: unknown = await response.json()
      if (ended || typeof value !== 'object' || value === null) return
      const data = value as Record<string, unknown>
      if (!['disabled', 'idle', 'running', 'waiting'].includes(String(data.state))) return
      state = String(data.state)
      confirmationId = typeof data.confirmation_id === 'string' ? data.confirmation_id : null
      sources = Array.isArray(data.sources) ? data.sources.filter((item): item is { label: string; source_id: string } => (
        typeof item === 'object' && item !== null && typeof item.label === 'string' && typeof item.source_id === 'string'
      )) : []
    } catch {
      // 通信復旧を待つ。取得済み出典をエラー本文で置換しない。
    } finally {
      if (!ended) timer = setTimeout(() => { void refresh() }, 1000)
    }
  }

  function stopRequest() {
    return fetch('/api/tool-use/stop', {
      method: 'POST', keepalive: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ character, conversation_id: conversationId }),
    })
  }

  async function stop() {
    stopping = true
    error = ''
    try {
      const response = await stopRequest()
      if (!response.ok) throw new Error('stop failed')
      await onStop()
      state = 'idle'
    } catch {
      error = '停止を確認できませんでした。もう一度停止してください。'
    } finally {
      stopping = false
    }
  }

  function leave() {
    void stopRequest().catch(() => undefined)
  }

  onMount(() => {
    void refresh()
    window.addEventListener('pagehide', leave)
    window.addEventListener('offline', leave)
    return () => {
      window.removeEventListener('pagehide', leave)
      window.removeEventListener('offline', leave)
    }
  })
  onDestroy(() => {
    ended = true
    controller.abort()
    clearTimeout(timer)
    // 会話切替と画面終了で、以前の会話の入力待ちを持ち越さない。
    void stopRequest().catch(() => undefined)
  })
</script>

{#if state === 'running' || state === 'waiting' || sources.length > 0}
  <section class="tool-use-status" aria-label="外部参照" aria-live="polite">
    {#if state === 'running'}
      <span>外部情報を確認しています…</span>
    {:else if state === 'waiting'}
      {#if confirmationId !== null}
        {#key confirmationId}
          <ActionConfirmation {character} {conversationId} requestId={confirmationId} {onContinue} />
        {/key}
      {:else}
        <span>追加情報をお待ちしています。入力または音声で回答できます。</span>
      {/if}
    {/if}
    {#if sources.length > 0}
      <details>
        <summary>今回参照した情報</summary>
        <ul>{#each sources as source}<li>{source.label}</li>{/each}</ul>
      </details>
    {/if}
    {#if state === 'running' || state === 'waiting'}
      <button type="button" disabled={stopping} on:click={stop}>外部操作を停止</button>
    {/if}
    {#if error}<p role="alert">{error}</p>{/if}
  </section>
{/if}

<style>
  .tool-use-status { flex: 0 0 100%; box-sizing: border-box; padding: 0.5rem 1rem; font-size: 0.875rem; }
  button { margin: 0.25rem 0.5rem; }
  ul { margin: 0.25rem 0; }
</style>
