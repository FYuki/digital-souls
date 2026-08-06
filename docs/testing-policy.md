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
npm run lint:python
npm run check
npm run build
```

`npm run test:integration:backend` は ChromaDB パッケージ、Ollama、`nomic-embed-text:latest` モデルを必要とする。

CI は単体テスト、結合テスト、モックE2E、型チェック、ビルドを実行する。実接続スイートは外部サービスを必要とするため自動実行せず、Pull Request の検証欄へローカル実行結果または未実行状態を記録する。

## LLM classifier conformance

意味分類器の品質評価は、通常のpytest unit testと分離したpromptfoo suiteで行う。

- pytest unitはprompt組立、schema、parser、fail-closed、決定論的evaluatorをfakeで検証する
- promptfooは固定した合成caseで実modelとproduction providerを評価する
- 実ユーザー本文をcase、結果、logへコピーしない
- 機微caseが`NOT_SENSITIVE`になることを許容せず、判定不能は`ABSTAIN`として保存を拒否する
- model、prompt、policyのversionを固定し、重要な変更は対象suiteを3回反復してから全suiteを実行する
- 外部serviceを必要とするため通常CIへ混在させず、release時の実接続証跡として扱う

配置、prompt tuningとproduction conformanceの分離、合格基準は
`docs/decisions/wave2-memory-formation-retrieval-2026-08.md`を正本とする。

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

正常に関連付けられた `evidence.json` は `suite`、`testLayer`、`profile`、`testStatus`、`runId`、`environmentReport` を記録する。`runId` は同じディレクトリの `environment-run.json` と一致しなければならず、Profile名も実行スイートと一致しなければならない。Profile解決に成功した場合は `profileReport` も記録する。環境ライフサイクルまたはPlaywrightテストが失敗した場合は、`failureCategory` に `profile`、`preparation`、`startup`、`readiness`、`supervision`、`test`、`teardown` のいずれかを記録する。

証跡生成処理自体が失敗した場合も `evidence.json` を保存する。この失敗証跡は `suite`、`testLayer`、`profile`、`testStatus` に加えて、`evidenceStatus: "failed"`、`failureCategory: "evidence"`、`failureStage`、`failureMessage` を記録する。`testStatus` はPlaywrightテストの成否だけを表し、`passed` であっても `evidenceStatus: "failed"` の証跡を実接続成功として扱わない。

`failureStage` は失敗した処理に応じて次のいずれかとする。

- `test-result`: `environment_cli.py test-result` の実行
- `environment-report-read`: `environment-run.json` の読込
- `environment-report-json`: JSON解析
- `environment-report-validation`: environment reportの契約検証
- `environment-report-association`: 実行スイートとProfileの関連付け

証跡生成失敗時は関連付けが完了していないため、`runId`、`profileReport`、`environmentReport` を記録しない。`failureMessage` には失敗原因のエラーメッセージを記録し、reporterは失敗証跡の保存後も呼び出し元へ失敗を伝える。
