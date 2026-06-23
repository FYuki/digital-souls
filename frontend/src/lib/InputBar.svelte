<script lang="ts">
  export let onSend: (message: string) => void
  export let disabled = false

  let text = ''

  const submit = () => {
    const message = text.trim()

    if (message.length === 0 || disabled) {
      return
    }

    onSend(message)
    text = ''
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
  <input
    bind:value={text}
    disabled={disabled}
    aria-label="メッセージ"
    placeholder="光織に話しかける"
    on:keydown={submitOnEnter}
  />
  <button type="submit" disabled={disabled || text.trim().length === 0}>送信</button>
</form>

<style>
  .input-bar {
    display: flex;
    gap: 12px;
    padding: 16px 24px 20px;
    border-top: 1px solid rgba(144, 67, 47, 0.16);
    background: #fff4ec;
  }

  input {
    flex: 1;
    min-width: 0;
    padding: 12px 14px;
    border: 1px solid rgba(144, 67, 47, 0.28);
    border-radius: 8px;
    color: #34201d;
    background: #fffdfa;
  }

  input:focus {
    outline: 3px solid rgba(185, 79, 56, 0.22);
    border-color: #b94f38;
  }

  button {
    flex: 0 0 auto;
    min-width: 82px;
    padding: 0 18px;
    border: 0;
    border-radius: 8px;
    color: #fffaf6;
    background: #b94f38;
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
    }

    button {
      min-width: 68px;
      padding: 0 12px;
    }
  }
</style>
