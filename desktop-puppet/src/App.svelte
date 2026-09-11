<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";
  import { mountLive2D } from "./live2d";

  type Message = {
    role: "user" | "assistant";
    text: string;
  };

  let canvas: HTMLCanvasElement;
  let live2dStatus = "initializing";
  let live2dDetail = "Live2D初期化待ち";
  let renderer: { destroy: () => void } | null = null;
  let displayMode: "live2d" | "fixed" = "live2d";
  let menuOpen = false;
  let chatOpen = false;
  let input = "";
  let messages: Message[] = [
    { role: "assistant", text: "Desktop Puppet PoCを起動しました。" },
  ];

  $: visibleMessages = messages.slice(-3);

  onMount(async () => {
    await tick();
    try {
      renderer = await mountLive2D(canvas, (status, detail) => {
        live2dStatus = status;
        live2dDetail = detail ?? "";
      });
    } catch (error) {
      live2dStatus = "error";
      live2dDetail = error instanceof Error ? error.message : String(error);
    }
  });

  onDestroy(() => {
    renderer?.destroy();
  });

  function toggleMenu(event: MouseEvent) {
    event.preventDefault();
    menuOpen = !menuOpen;
  }

  function sendMessage() {
    const text = input.trim();
    if (!text) return;

    messages = [...messages, { role: "user", text }];
    input = "";

    window.setTimeout(() => {
      messages = [
        ...messages,
        { role: "assistant", text: `PoC応答: 「${text}」を受け取りました。` },
      ];
    }, 300);
  }
</script>

<svelte:window on:click={() => (menuOpen = false)} />

<main class="puppet-shell" on:contextmenu={toggleMenu}>
  <div class="drag-handle" data-tauri-drag-region title="ドラッグして移動">⋮⋮</div>

  <section class="puppet-stage" class:fixed={displayMode === "fixed"}>
    <canvas bind:this={canvas} class:hidden={displayMode !== "live2d"}></canvas>

    {#if displayMode === "fixed"}
      <div class="fixed-portrait" aria-label="固定立ち絵プレースホルダー">
        <div class="portrait-glow"></div>
        <div class="portrait-face">光織</div>
        <div class="portrait-caption">FIXED PORTRAIT PoC</div>
      </div>
    {/if}

    <div class:ready={live2dStatus === "ready"} class:error={live2dStatus === "error"} class="status-pill">
      <strong>{live2dStatus === "ready" ? "Live2D READY" : live2dStatus.toUpperCase()}</strong>
      <span>{live2dDetail}</span>
    </div>
  </section>

  {#if chatOpen}
    <section class="chat-panel" on:click|stopPropagation>
      <div class="chat-history">
        {#each visibleMessages as message}
          <div class:user={message.role === "user"} class:assistant={message.role === "assistant"} class="message">
            <span class="role">{message.role === "user" ? "You" : "光織"}</span>
            <span>{message.text}</span>
          </div>
        {/each}
      </div>
      <form on:submit|preventDefault={sendMessage}>
        <input bind:value={input} placeholder="メッセージを入力" autocomplete="off" />
        <button type="submit">送信</button>
      </form>
      <p class="poc-note">PoCではローカル模擬応答。BE接続は次段階で既存Conversation Coreへ接続する。</p>
    </section>
  {/if}

  {#if menuOpen}
    <div class="context-menu" on:click|stopPropagation>
      <button on:click={() => (chatOpen = !chatOpen)}>{chatOpen ? "テキスト入力を閉じる" : "テキスト入力を開く"}</button>
      <button on:click={() => (displayMode = "live2d")}>Live2D表示</button>
      <button on:click={() => (displayMode = "fixed")}>固定立ち絵表示</button>
    </div>
  {/if}
</main>
