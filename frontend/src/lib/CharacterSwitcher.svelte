<script lang="ts">
  export let currentCharacter: string
  export let disabled: boolean
  export let onSwitch: (character: string) => void

  let draftCharacter = currentCharacter

  $: normalizedCharacter = draftCharacter.trim()

  const handleSubmit = () => {
    if (disabled || normalizedCharacter.length === 0) {
      return
    }

    onSwitch(normalizedCharacter)
    draftCharacter = normalizedCharacter
  }
</script>

<form class="character-switcher" on:submit|preventDefault={handleSubmit}>
  <label for="character-id">キャラクターID</label>
  <div class="character-switcher-controls">
    <input id="character-id" bind:value={draftCharacter} {disabled} />
    <button type="submit" disabled={disabled || normalizedCharacter.length === 0}>切り替え</button>
  </div>
</form>

<style>
  .character-switcher {
    display: grid;
    gap: 4px;
    margin-top: 12px;
  }

  label {
    color: #6f3a2d;
    font-size: 0.78rem;
    font-weight: 700;
  }

  .character-switcher-controls {
    display: flex;
    gap: 8px;
  }

  input {
    min-width: 0;
    flex: 1;
    padding: 7px 9px;
    border: 1px solid rgba(144, 67, 47, 0.3);
    border-radius: 6px;
  }

  button {
    padding: 7px 12px;
    border: 0;
    border-radius: 6px;
    color: #fff;
    background: #9f4933;
    cursor: pointer;
  }

  button:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }
</style>
