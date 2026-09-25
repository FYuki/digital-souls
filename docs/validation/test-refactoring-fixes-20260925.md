# #480後のテストリファクタリング修正（2026-09-25）

基点は `origin/main` の `3f82389e3bf561b81620c308aee69ab6419ba245`。専用 worktree `test-refactoring-after-480` の `codex/test-refactoring-after-480` で、再評価で維持した T1〜T10 を修正した。

| ID | 修正 |
|---|---|
| T1 | Vitest の収集を unit/module 命名ファイルに限定し、通常コマンドと CI の対象を一致させた。 |
| T2 | 承認待機テストの60秒実時間待ちを短い検証用期限に置き換え、既定値と期限後の振る舞いは維持した。 |
| T3 | pytest/Vitest の JUnit と所要時間、Playwright の失敗時 trace/screenshot、mocked E2E の環境 report を CI artifact に保存する。 |
| T4 | unit/module の外向き socket 接続を拒否し、loopback を許可。合成 HTTP と episode 出力の例外は明示 marker で管理する。 |
| T5 | 実装文字列の照合を減らし、起動結果・公開面・資源境界・scanner の振る舞いで検証する。 |
| T6 | crash 子 process を spawn と期限付き join に変更し、所有する子だけを終了・回収する。 |
| T7 | 音声品質の純粋な判定と artifact／Git履歴／Node replay の監査を分け、後者を専用 CI job で実行する。 |
| T8 | Core lifecycle、LiveKit audio、WS、dogfood deploy、lifespan と、Frontend の App、LiveKit room、voice history の大きなテストを責務ごとに分割した。 |
| T9 | Python 3.12／Linux の constraints、`ubuntu-24.04` runner、Python/Node/OS/依存版の記録を導入した。 |
| T10 | 起動テストと signal harness の port、Compose project/container 名を run ごとに分離し、起動失敗時に report の原因を表示する。 |

## ローカル検証

- Backend unit: **4,606成功、1 skip、2除外**。除外2件は `cross_language` job へ分離したもの。
- Backend module: **2,022成功、54除外**。除外54件は `cross_language` job へ分離したもの。起動・停止の実CLIテストも通過。
- Backend cross-language: **56成功**。従来の artifact・履歴・Node replay の監査を実行。
- Frontend: 汎用 Vitest **1,206成功、収集エラーなし**。CI と同じ分割コマンドでは unit **1,018成功**、module **188成功**。JUnit を生成。
- Frontend check、build、共有 voice-session 生成差分チェック、Backend mypy、実装側変更箇所の Ruff、`pip check`、CI YAML 解析、Compose config、`git diff --check` は成功。
- Python constraints は requirements と合わせた `pip install --dry-run --ignore-installed` に成功。完全な新規環境へのインストールや Docker image build の代用ではない。

既存サービスが 4174/5173 を使用しているため、ローカルの mocked E2E は実行していない。Docker image build と変更後の GitHub Actions 実行も未実行であり、CI 上の runner・依存版と実行時間は branch の CI で確認する。実サービス、実マイク、GPU 受入はこの変更の検証に含めない。T11 の外部ダウンロード一時障害は本件の10項目とは別の提案として残る。
