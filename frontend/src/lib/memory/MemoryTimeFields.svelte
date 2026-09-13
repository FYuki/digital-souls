<script lang="ts">
  import type { TimeParts } from './episodic-client'
  export let value: TimeParts
  export let label: string
  const fields = [
    ['year', '年', 1, 9999], ['month', '月', 1, 12], ['day', '日', 1, 31],
    ['hour', '時', 0, 23], ['minute', '分', 0, 59], ['second', '秒', 0, 59],
  ] as const
</script>

<fieldset>
  <legend>{label}（不明な欄は空欄）</legend>
  <div>
    {#each fields as [key, name, min, max]}
      <label>{name}
        <input type="number" aria-label={`${label} ${name}`} {min} {max} step="1"
          value={value[key] ?? ''}
          on:input={(event) => { value = { ...value, [key]: event.currentTarget.value === '' ? null : event.currentTarget.valueAsNumber } }} />
      </label>
    {/each}
  </div>
</fieldset>

<style>
  fieldset { border: 1px solid #574d63; border-radius: 8px; margin: 12px 0; min-width: 0; }
  div { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
  label { display: grid; gap: 4px; }
  input { box-sizing: border-box; width: 100%; min-height: 44px; border: 1px solid #574d63; border-radius: 6px; background: #100d17; color: #fff; padding: 6px; }
  input:focus-visible { outline: 2px solid #f0a3c1; }
</style>
