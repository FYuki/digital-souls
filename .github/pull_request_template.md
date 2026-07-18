## 検証

- [ ] `npm run test:unit`
- [ ] `npm run test:module`
- [ ] `npm run test:e2e:mocked`

実接続の確認が必要な変更では、必要な外部サービスを起動して結果または未実行理由を記録する。

- [ ] `npm run test:integration:backend`（ChromaDB、Ollama、`nomic-embed-text:latest`）— 結果: 未実行
- [ ] `npm run test:integration:text`（Backend、Ollama）— 結果: 未実行
- [ ] `npm run test:integration:voice`（Backend、Ollama、VOICEVOX、Whisper）— 結果: 未実行

モックE2Eの成功は、実接続インテグレーションテスト成功の証跡には含めない。
