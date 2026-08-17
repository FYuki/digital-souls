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
- classifier: `semantic-privacy-classifier-v1`
- prompt: `semantic-privacy-prompt-v2`
- policy: `backend/app/memory/memory_policy.json` の `policy_version`

比較結果には上記4種類の provenance を併記し、いずれかが異なる実行を同一条件として集計しない。`provider.py` は評価プロセスの起動時に policy と model digest を一度だけ解決する。

## 品質指標と暫定閾値

- `abstain_rate`: 全ケースのうち`ABSTAIN`になった割合
- `false_negative_rate`: 機微ケースを`NOT_SENSITIVE`と判定した割合
- `false_positive_rate`: 非機微ケースを`SENSITIVE`と判定した割合

`thresholds.json`の各50%は評価基盤を導入するための暫定基準であり、最終的な品質目標ではない。
後続issueでプロンプトを調整し、少なくとも偽陽性率20%以下を目標として段階的に厳格化する。
後続issueの起票後に、本節へissue番号と確定した各指標の目標値を追記する。
