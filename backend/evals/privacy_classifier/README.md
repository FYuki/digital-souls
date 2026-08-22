# SemanticPrivacyClassifier 評価

このディレクトリは、すべて合成した日英ケースだけを使う手動評価入口である。実ユーザー本文、本文ハッシュ、モデル入出力全文、parser frame を corpus や共有成果物へ保存しない。評価結果も外部へ共有しない。

## 実行

Ollama で `gemma4:e4b` を利用可能にしたうえで、リポジトリルートから実行する。

```bash
npm run eval:privacy:prompt-lab
npm run eval:privacy:conformance
```

`prompt-lab.yaml` は Ollama を直接呼び、prompt 候補を比較する。`conformance.yaml` は `provider.py` を介して production の `OllamaSemanticPrivacyClassifier` を呼ぶ release gate である。標準の unit / module test には実 Ollama 評価を含めない。

## バージョン固定

- model: `OLLAMA_CLASSIFIER_MODEL`。既定は `gemma4:e4b`。起動時に `/api/show` から digest を解決する。
- classifier: `semantic-privacy-classifier-v2`
- prompt: `semantic-privacy-prompt-v11`
- policy: `backend/app/memory/memory_policy.json` の `policy_version`

比較結果には上記4種類の provenance を併記し、いずれかが異なる実行を同一条件として集計しない。`provider.py` は評価プロセスの起動時に policy と model digest を一度だけ解決する。

## 品質指標と受け入れ基準

- `abstain_rate`: 全ケースのうち`ABSTAIN`になった割合
- `false_negative_rate`: 機微ケースを`NOT_SENSITIVE`と判定した割合
- `false_positive_rate`: 非機微ケースを`SENSITIVE`と判定した割合

Issue #28のWave 2受け入れでは、ADMISSIONとQUERY_GATEをそれぞれ独立して評価し、
`abstain_rate` 10%未満、`false_negative_rate` 5%未満、`false_positive_rate` 20%未満を
目標とする。同一model digest、prompt version、policy versionかつcache無効・concurrency 1の
全case評価を3回実行し、各回が個別に目標を満たした場合だけ受け入れる。3回の平均値では判定しない。

`thresholds.json`には各指標の上限値を保持する。固定corpusは1 profileあたり60件であるため、
厳密な「未満」の最終判定ではABSTAINは最大5件、偽陰性は機微40件中最大1件、偽陽性は
非機微20件中最大3件となる。gateは集計値が境界値と一致した場合も不合格にする。

Issue #28の同一条件3回の受け入れ結果は`acceptance-2026-08-23.json`に保持する。
