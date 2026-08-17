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

- model: `OLLAMA_CHAT_MODEL`。既定は `gemma4:e4b`。起動時に `/api/show` から digest を解決する。
- classifier: `semantic-privacy-classifier-v1`
- prompt: `semantic-privacy-prompt-v1`
- policy: `backend/app/memory/memory_policy.json` の `policy_version`

比較結果には上記4種類の provenance を併記し、いずれかが異なる実行を同一条件として集計しない。`provider.py` は評価プロセスの起動時に policy と model digest を一度だけ解決する。
