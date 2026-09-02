<script lang="ts">
  import type { CharacterCatalogEntry } from './characters/types'

  export let character: CharacterCatalogEntry | null = null

  let failedUrl: string | null = null
  $: imageUrl = character?.standing_image.status === 'available'
    ? character.standing_image.url
    : null
  $: showImage = imageUrl !== null && failedUrl !== imageUrl
</script>

<div
  class="portrait"
  class:placeholder={!showImage}
  aria-label={showImage
    ? `${character?.display_name ?? 'キャラクター'}の立ち絵`
    : '立ち絵未設定'}
>
  {#if showImage}
    <img
      src={imageUrl}
      alt={`${character?.display_name ?? 'キャラクター'}の立ち絵`}
      on:error={() => { failedUrl = imageUrl }}
    />
  {:else}
    <div class="placeholder-figure" aria-hidden="true">
      <span class="placeholder-head"></span>
      <span class="placeholder-body"></span>
    </div>
    <p>{character?.display_name ?? 'Character'}</p>
    <span>立ち絵を準備中</span>
  {/if}
</div>

<style>
  .portrait {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: flex-end;
    justify-content: center;
    overflow: hidden;
    pointer-events: none;
  }

  img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    object-position: center bottom;
  }

  .placeholder {
    box-sizing: border-box;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 5px;
    padding: 24px;
    color: #8d8598;
    background:
      radial-gradient(circle at 50% 34%, rgba(240, 163, 193, 0.12), transparent 24%),
      linear-gradient(160deg, rgba(156, 130, 255, 0.08), transparent 54%);
  }

  .placeholder-figure {
    position: relative;
    width: 112px;
    height: 154px;
    opacity: 0.58;
  }

  .placeholder-head,
  .placeholder-body {
    position: absolute;
    left: 50%;
    display: block;
    border: 1px solid rgba(255, 255, 255, 0.18);
    background: rgba(255, 255, 255, 0.08);
    transform: translateX(-50%);
  }

  .placeholder-head { top: 8px; width: 54px; height: 62px; border-radius: 50%; }
  .placeholder-body { bottom: 0; width: 110px; height: 84px; border-radius: 55px 55px 16px 16px; }
  p { margin: 0; color: #b9b1c2; font-size: 0.82rem; font-weight: 700; }
  .placeholder > span { font-size: 0.68rem; }
</style>
