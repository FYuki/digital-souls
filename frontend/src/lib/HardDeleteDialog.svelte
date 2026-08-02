<script lang="ts">
  export let conversationId: string
  export let disabled: boolean
  export let onConfirm: () => void
  export let onCancel: () => void
</script>

<div class="backdrop" role="presentation">
  <section role="dialog" aria-modal="true" aria-labelledby="delete-title">
    <h2 id="delete-title">スレッドを完全に削除しますか</h2>
    <p>対象スレッド: {conversationId}</p>
    <p>短期会話履歴は失われ、削除後は復元できません。</p>
    <p>RAG長期記憶は削除されません。</p>
    <p>既存のbackup、snapshot、ファイルシステム上の複製からの消去を保証しません。</p>
    <div class="actions">
      <button type="button" on:click={onCancel} disabled={disabled}>キャンセル</button>
      <button type="button" class="danger" on:click={onConfirm} disabled={disabled}>完全に削除</button>
    </div>
  </section>
</div>

<style>
  .backdrop { position: fixed; inset: 0; z-index: 10; display: grid; place-items: center; background: rgba(40, 20, 16, 0.45); }
  section { width: min(480px, calc(100% - 32px)); box-sizing: border-box; padding: 24px; border-radius: 8px; background: white; }
  h2 { margin-top: 0; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; }
  button { padding: 8px 12px; }
  .danger { color: white; background: #9d281f; }
</style>
