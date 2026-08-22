# Wave 2 受け入れ結果（2026年8月）

## 結論

Issue #28のコード・品質受け入れ条件は、2026-08-23のローカル実行で合格した。
先行基盤#50と11件の子IssueはGitHub上ですべてclosedであり、HEADのdogfood profileは
Chromaを`real`として`RAG_ENABLED=true`を導出する。

本作業ではdogfoodへのdeployとGitHub Issue #28のcloseは行っていない。実会話データを保持する
環境への反映は、通常のbackup・deploy・rollback手順で別途実施する。

## 意味分類器

仮閾値50%を次の厳密な上限へ変更した。gateは境界値と一致した場合も不合格にする。

- ABSTAIN率: 10%未満
- 偽陰性率: 5%未満
- 擬陽性率: 20%未満

分類要求だけに`think=false`、`temperature=0`、分類結果の整合条件を表すJSON Schemaを指定した。
会話用Ollama要求は変更していない。promptは個人事実と一般質問、日英の主語・scope、暗示的兆候、
安全な嗜好と機微情報の混在を区別するv11とした。

実行条件はOllama 0.32.5、`gemma4:e4b`、model digest
`sha256:4c27e0f5b5adf02ac956c7322bd2ee7636fe3f45a8512c9aba5385242cb6e09a`、
classifier v2、prompt v11、policy `2026-08-wave2-v1`、cache無効、concurrency 1である。

| 回 | profile | case failure | ABSTAIN | 偽陰性 | 擬陽性 | median | p95 |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | ADMISSION | 0/60 | 0% | 0% | 0% | 0.612秒 | 0.690秒 |
| 1 | QUERY_GATE | 0/60 | 0% | 0% | 0% | 0.587秒 | 0.693秒 |
| 2 | ADMISSION | 0/60 | 0% | 0% | 0% | 0.606秒 | 0.659秒 |
| 2 | QUERY_GATE | 0/60 | 0% | 0% | 0% | 0.577秒 | 0.623秒 |
| 3 | ADMISSION | 0/60 | 0% | 0% | 0% | 0.602秒 | 0.663秒 |
| 3 | QUERY_GATE | 0/60 | 0% | 0% | 0% | 0.574秒 | 0.612秒 |

3回は同一provenanceであり、平均ではなく各回が個別に合格した。機械可読な集計は
`backend/evals/privacy_classifier/acceptance-2026-08-23.json`を参照する。

## Wave 2横断確認

Ubuntu-devの開発依存と、Ubuntu-dogfoodの実Ollama・VOICEVOX・Whisperを使用した。
実接続用データは独立した一時data rootを使い、dogfoodの会話履歴とpersona memoryへ接続していない。

| 確認 | 結果 |
|---|---|
| backend unit | 1517 passed |
| frontend unit | 153 passed |
| backend module | 1289 passed |
| frontend module | 89 passed |
| backend integration（実Ollama・実Chroma） | 3 passed |
| frontend mocked E2E | 20 passed |
| frontend integration text（実BE・FE・Ollama・Chroma） | 1 passed |
| frontend integration voice（実BE・FE・Ollama・VOICEVOX・Whisper） | 6 passed |
| mypy | 144 source files、問題なし |
| Svelte／TypeScript check | error 0、warning 0 |
| Ruff | 成功 |
| frontend production build | 成功、106 modules |
| `git diff --check` | 成功 |

実Chroma integrationでは固定manifest、SQLite正本からの再構築、改ざんされたChroma本文を採用しない
取得経路を通過した。mocked E2Eでは記憶管理、会話履歴privacy metadata、text／voiceの既存UI回帰を
確認した。

## 運用観測事項

`integration-text`の最初のquery gateは、Ollamaがcoldの状態で2秒timeoutとなり、安全側にRAG検索を
skipした。その後の会話応答と統合テストは成功しており、機微情報が検索へ流れる失敗ではない。
一方、長時間idle後の最初の会話では記憶検索が1回欠落し得るため、運用開始後はclassifier timeout率、
model待ち時間、query gateのABSTAIN率をmetadata-onlyで監視する。継続的に発生する場合はmodel warmup、
keep-alive、または専用instanceを別変更として検討する。
