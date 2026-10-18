# Client

Core に接続するクライアント（Web の FE と、それを包むネイティブアプリ）を置くディレクトリです。

まだ実装はありません。構成は ADR 0035（Proposed、[PR #222](https://github.com/FYuki/digital-souls/pull/222)）で提案しています。

- Web の FE は Svelte＋Vite＋TypeScript で新しく作ります。PoC の FE はコピー・フォークしません。
- ネイティブアプリは、同じ FE のビルドを Tauri で包みます。
- Card・プロンプト・会話履歴は Core だけが持ち、クライアントは持ちません。
- Core の範囲（[README](../README.md) の「製品の範囲」）は変えません。

言語・toolchain・lockfile・CI の品質ゲートは、実装を追加するときに [CONTRIBUTING](../CONTRIBUTING.md) の
「製品の品質ゲート」に従って整備します。
