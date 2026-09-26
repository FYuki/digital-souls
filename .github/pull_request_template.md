## 変更内容

## ドキュメント整合

- [ ] 用語・公開API・schema・責務境界の変更を`CONTEXT.md`へ反映した、または対象外の理由を記載した
- [ ] README・AGENTS・アーキテクチャ・関連運用手順の更新要否を確認した
- [ ] 実装済み／一部実装／設計のみ、既定有効／任意有効化、今回の検証／過去の証跡を区別した

文書のみの変更では、相対リンク・参照する実装名・Markdownの確認結果を記録する。

## 検証

- [ ] `npm run test:unit`
- [ ] `npm run test:module`
- [ ] `npm run test:e2e:mocked`

実接続の確認が必要な変更では、必要な外部サービスを起動して結果または未実行理由を記録する。
文書のみの変更でアプリケーションテストを実行しない場合も、その理由を記録する。

- [ ] `npm run test:integration:backend`（ChromaDB、Ollama、`nomic-embed-text:latest`）— 結果: 未実行
- [ ] `npm run test:integration:text`（Backend、Ollama）— 結果: 未実行
- [ ] `npm run test:integration:voice`（Backend、LiveKit、Ollama、VOICEVOX、Whisper）— 結果: 未実行

モックE2Eの成功は、実接続インテグレーションテスト成功の証跡には含めない。
