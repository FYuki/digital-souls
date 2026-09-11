<script lang="ts">
  import type {TextSubmission} from '../livekit/text-input'
  export let onSend: (message: string, screenReference?: boolean) =>
    void | Promise<'accepted' | 'failed' | {inputId: string}>
  export let disabled = false
  export let sendDisabled = false
  export let characterName = '光織'
  export let screenReferenceAvailable = false
  export let threadKey = 'default'
  export let submission: TextSubmission | null = null
  export let onFocusChanged: (focused: boolean) => void = () => undefined

  let text = ''
  let screenReference = false
  let inputElement: HTMLInputElement
  let activeKey = threadKey
  let submitting = false
  let pendingInputId: string | null = null
  let handledInputId: string | null = null
  const drafts = new Map<string, string>()

  $: if (!screenReferenceAvailable) screenReference = false
  $: if (activeKey !== threadKey) {
    drafts.set(activeKey, text)
    activeKey = threadKey
    text = drafts.get(threadKey) ?? ''
    pendingInputId = null
    handledInputId = null
    submitting = false
    screenReference = false
    inputElement?.blur()
  }
  $: pending = submitting || submission?.status === 'sending' || submission?.status === 'confirming'
  $: if (submission !== null && submission.inputId !== handledInputId) {
    if (submission.status === 'sending' || submission.status === 'confirming') {
      text = submission.text
      pendingInputId = submission.inputId
    } else if (submission.inputId === pendingInputId) {
      handledInputId = submission.inputId
      pendingInputId = null
      if (submission.status === 'accepted') {
        text = ''
        screenReference = false
        inputElement?.blur()
      } else {
        text = submission.text
        inputElement?.focus()
      }
    }
  }

  const submit = async () => {
    const message = text.trim()

    if (message.length === 0 || disabled || sendDisabled || pending) {
      return
    }
    const key = threadKey
    submitting = true
    try {
      const result = await (screenReference ? onSend(message, true) : onSend(message))
      if (threadKey !== key) return
      if (typeof result === 'object') pendingInputId = result.inputId
      else if (result === 'failed') inputElement?.focus()
      else {
        text = ''
        screenReference = false
        inputElement?.blur()
      }
    } catch {
      if (threadKey === key) inputElement?.focus()
    } finally {
      if (threadKey === key) submitting = false
    }
  }

  const submitOnEnter = (event: KeyboardEvent) => {
    if (event.key !== 'Enter' || event.isComposing) {
      return
    }

    event.preventDefault()
    submit()
  }
</script>

<form class="input-bar" on:submit|preventDefault={submit}>
  <label class="screen-reference">
    <input
      type="checkbox"
      bind:checked={screenReference}
      disabled={disabled || pending || !screenReferenceAvailable}
    />
    現在の画面を参照
  </label>
  <input
    bind:this={inputElement}
    bind:value={text}
    disabled={disabled}
    readonly={pending}
    aria-label="メッセージ"
    placeholder={`${characterName}に話しかける`}
    on:keydown={submitOnEnter}
    on:focus={() => onFocusChanged(true)}
    on:blur={() => onFocusChanged(false)}
  />
  <button type="submit" disabled={disabled || sendDisabled || pending || text.trim().length === 0}>送信</button>
  {#if submission?.status === 'confirming'}
    <span role="status">送信確認中</span>
  {:else if pending}
    <span role="status">送信中</span>
  {:else if submission?.status === 'rejected' || submission?.status === 'not_received'}
    <span role="status">送信されていません。再送できます。</span>
  {/if}
</form>

<style>
  .input-bar {
    display: flex;
    gap: 12px;
    padding: 16px 24px 20px;
    border-top: 1px solid rgba(255, 255, 255, 0.09);
    background: #15111d;
  }

  .screen-reference {
    display: flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
    color: #d9d1df;
    font-size: 0.82rem;
  }

  .screen-reference input { flex: 0 0 auto; }

  input:not([type='checkbox']) {
    flex: 1;
    min-width: 0;
    padding: 12px 14px;
    border: 1px solid #4c4358;
    border-radius: 10px;
    color: #f8f3ff;
    background: #100d17;
  }

  input:not([type='checkbox']):focus {
    outline: 3px solid rgba(240, 163, 193, 0.2);
    border-color: #d98bac;
  }

  button {
    flex: 0 0 auto;
    min-width: 82px;
    padding: 0 18px;
    border: 0;
    border-radius: 10px;
    color: #fff8fb;
    background: #9d496b;
    font-weight: 700;
    cursor: pointer;
  }

  button:disabled,
  input:disabled {
    cursor: not-allowed;
    opacity: 0.58;
  }

  @media (max-width: 480px) {
    .input-bar {
      padding: 12px;
      gap: 8px;
      flex-wrap: wrap;
    }

    .screen-reference { flex-basis: 100%; }

    button {
      min-width: 68px;
      padding: 0 12px;
    }
  }
</style>
