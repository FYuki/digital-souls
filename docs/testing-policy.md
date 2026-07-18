# テスト方針

本書は `digital-souls` 固有のテスト層、配置、実行方法、実行証跡を定める。

## テスト層

| 層 | 配置・命名 | 外部サービス |
|---|---|---|
| 単体テスト | Backend: `backend/tests/unit/`、Frontend: `*.unit.test.ts` | モック可 |
| 結合テスト | Backend: `backend/tests/module/`、Frontend: `*.module.test.ts` | 境界をモック可 |
| モックE2E | `frontend/e2e/` | Browser 内 Backend をモック |
| インテグレーションテスト | Backend: `backend/tests/integration/test_*_integration.py`、Frontend: `frontend/integration/` | 実接続必須 |

「結合テスト」はアプリ内のモジュール横断を検証する。「インテグレーションテスト」は Ollama、ChromaDB、VOICEVOX、Whisper などの外部サービスへ実接続するテストだけを指す。Backend では ChromaDB と Ollama の実埋め込み API を使う RAG runtime evidence テストをインテグレーションテストとして実行する。

モックを使用する単体・結合・E2Eテストの結果は、外部サービスとの実接続に成功した一次証跡として扱わない。

## Playwright スイート

| コマンド | 配置 | Profile | 要求Capability | 結果ディレクトリ |
|---|---|---|---|---|
| `npm run test:e2e:mocked` | `frontend/e2e/` | `test-mocked` | `mocked-e2e` | `frontend/test-results/mocked-e2e/` |
| `npm run test:integration:text` | `frontend/integration/text/` | `integration-text` | `text-chat-real` | `frontend/test-results/integration-text/` |
| `npm run test:integration:voice` | `frontend/integration/voice/` | `integration-voice` | `voice-chat-real` | `frontend/test-results/integration-voice/` |

各設定は Profile、収集ディレクトリ、成果物の出力先を固定する。spec 内で環境変数や依存 mode によってモックと実接続を切り替えない。各 spec が受け入れる要求Capabilityは1つだけとする。実接続 spec では mock WebSocket、`page.route`、HARによる外部通信の置換を禁止する。

## 実行入口

リポジトリルートから次を実行する。

```text
npm run test:unit
npm run test:module
npm run test:integration:backend
npm run test:e2e:mocked
npm run test:integration:text
npm run test:integration:voice
```

`npm run test:integration:backend` は ChromaDB パッケージ、Ollama、`nomic-embed-text:latest` モデルを必要とする。

CI は単体テスト、結合テスト、モックE2E、型チェック、ビルドを実行する。実接続スイートは外部サービスを必要とするため自動実行せず、Pull Request の検証欄へローカル実行結果または未実行状態を記録する。

## Capability不足と失敗

スイートの要求Capabilityが resolved Profile にない場合、テストは不足Capabilityと解決済み依存を理由に `skip` する。スイートを明示的に開始した後の次の失敗は skip に変換しない。

- Profile 解決失敗: `profile`
- 環境準備・起動失敗: `preparation` または `startup`
- readiness 失敗: `readiness`
- Playwright テスト失敗: `test`

環境ライフサイクルの詳細と失敗カテゴリは `environment-run.json` に保持する。

## スイート別証跡

各結果ディレクトリには次を保存し、別スイートの成果物を上書きしない。

- `playwright-results.json`
- `resolved-profile.json`
- `environment-run.json`
- `evidence.json`

`evidence.json` は `suite`、`testLayer`、`profile`、`testStatus`、レポートの相対パスを記録する。`runId` は同じディレクトリの `environment-run.json` と一致しなければならず、Profile名も実行スイートと一致しなければならない。失敗時は `failureCategory` により Profile・環境・readiness・テストの失敗を区別する。
