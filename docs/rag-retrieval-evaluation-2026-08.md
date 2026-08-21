# RAG検索品質評価（2026年8月）

## 採用条件

決定的評価の正本は`backend/evals/rag_retrieval/manifest.json`とする。固定embeddingは
`fixed-vector-rag-eval-v1`（digest
`sha256:4bca86f7266e66c973d16a92f3cf0b033a92791f350536b86eb748c6f6b3e56a`）、距離はsquared L2、
relevanceは`1 / (1 + sqrt(distance))`である。

採用値は候補20件、最大注入5件、relevance threshold 0.05、同等margin 0.002である。
metadataの正本はSQLiteであり、`character_id`、`provider_id=core`、`status=ACTIVE`、TTL、
policy version、決定論的privacy scanを再検証する。Chroma metadataだけでは採用しない。

`budget.rag`は新設せず既存のprompt入力上限を使う。標準設定では
`OLLAMA_CONTEXT_TOKENS(8192) - ASSISTANT_MAX_GENERATION_TOKENS(1024) = 7168 token`であり、
region上限またはtotal上限を超えた場合はranking済みprefixを保持して下位から除外する。

実連携評価は`nomic-embed-text:latest`を`resolve_ollama_embedding_model()`で解決し、実Chromaと
実Ollamaを使う。通常CIから分離し、`npm run eval:rag:real`で明示実行する。

## 決定的評価結果

- 実行日時: 2026-08-21（UTC）
- 実行条件: Python 3.12.3、fixed-vector-rag-eval-v1、manifest schema v1、9 cases
- recall: 1.0
- precision: 1.0
- 無関係memory混入率: 0.0
- tie-break正解率: 1.0（100%）
- privacy境界違反: 0件
- character境界違反: 0件
- threshold未満混入: 0件
- 未検証memoryへのfallback: 0件
- 取得context: 9 items。synthetic層は実tokenizerを使わないためtoken数は実連携評価で記録する
- latency: manifest全9 casesの1000回平均0.179 ms（固定vector計算、SQLite条件再検証相当、rankingを含む）

## 実連携評価結果

- 実行日時: 2026-08-21（JST）
- Ollama: 0.32.5
- embedding: `nomic-embed-text:latest`
- model digest: `0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f`
- recall / precision: 1.0 / 1.0
- 無関係memory混入率: 0.0
- tie-break正解率: 1.0（100%）
- privacy・character境界違反、threshold未満混入、未検証fallback: 各0件
- latency（9 cases合計）: 実embedding＋Chroma検索2343.86 ms、SQLite契約再検証0.04 ms、
  ranking 0.20 ms、prompt用RAG message合成0.01 ms
- context token使用量: 9 itemsを`gemma4:e4b`の実tokenizerで一括計測し147 token

manifest runnerのSQLite契約再検証は固定recordに対する決定的評価である。実SQLiteからの再取得と
Chroma改ざん本文を採用しない結合は、同じopt-in integration suiteのruntime evidence testで確認した。
latencyとcontext token使用量は環境・modelに依存する記録値でありgateにはしない。今回のdefault有効化は、
決定的層と実連携層の双方がhard gateを満たした結果に基づく。

## 実行方法

通常CIの決定的層は`python3 -m pytest backend/tests/unit/test_rag_retrieval_eval_assets.py`、実連携層は
`npm run eval:rag:real`で実行する。実連携層はChromaと、取得済みのOllama embedding modelを必要とする。
