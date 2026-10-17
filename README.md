# Digital Souls

キャラクターとの継続的な会話のための、プロバイダーに依存しない最小限のCoreと、そのクライアントを開発する公開リポジトリです。
2026-10-18に`FYuki/digital-souls-core`から`FYuki/digital-souls`へ改名しました。

## リポジトリ構成

| パス | 内容 |
| --- | --- |
| `core/` | Core（Python、FastAPI）。`src`・`tests`・`evals`・`examples`・`characters`・Coreの`tools`、`pyproject.toml`・`uv.lock` |
| `client/` | クライアント（WebのFEとネイティブの包装）。[client](client/README.md)を参照 |
| `docs/` | Coreの文書・ADR・証跡（リポジトリ全体の決定を含む） |
| `tools/` | リポジトリ全体の文書検査と必須テスト用reporter |

Coreのコマンド（`uv`、`core/tools`、評価、Compose）は`core/`で実行します。`docs/`などの文書で
`src/…`・`tests/…`・`evals/…`・`tools/evaluate-…`等と書いたパスは、`core/`からの相対パスです。
証跡（`docs/evidence`）のコマンドとパスは記録時点の構成（リポジトリ直下）のままです。

開発・検証は[CONTRIBUTING](CONTRIBUTING.md)、用語は[CONTEXT](CONTEXT.md)、
記憶・人格の仕様と実装状況は[SPEC](SPEC.md)を参照してください。エージェント向けの入口は
[AGENTS](AGENTS.md)です。決定は[ADR](docs/adr/README.md)、進捗は
[Issue](https://github.com/FYuki/digital-souls/issues)、検証結果は
[日付付き証跡](docs/evidence/2026-10-02-bootstrap.md)で管理します。

## 製品の範囲

このリポジトリでは、小さく、プロバイダーに依存しないCoreを開発します。Coreは、キャラクターとの
継続的な会話・記憶・人格を一つの境界で扱い、複数の入口（チャットUI、外部Agent等）から同じ人格を
利用できるようにします。

製品利用は、一つの常駐Coreの共通runtimeへ各入口から接続する方針です。
CLIを追加する場合は薄いクライアントとします（[ADR 0025](docs/adr/0025-shared-runtime-interfaces.md)）。
現在の外部入口はHTTP APIで、CLI・MCPの追加は未実装です。
OpenClaw固有の入力由来・履歴連携は[連携検討](docs/openclaw-integration-design.md)で扱います。

Coreの範囲は次のとおりです。

- キャラクター定義と、対話に必要なコンテキスト。
- プライバシー判断と、保存・検索・外部送信のポリシー。
- 会話履歴と、記憶の形成・訂正・削除・検索。
- 経験からの内省・人格・関係の更新（[ADR 0020](docs/adr/0020-reflection-personality-relationship.md)）。
- プロバイダーのadapterを交換できるLLM port。

音声・STT・TTS、LiveKit、UIはCoreの対象外です。通常チャットUIからも呼べます（[推論API](docs/api.md)）。
会話中のツール利用（ツール一覧・Execution Gate・流出防止・承認）と、会話外の活動の判断と状態はCoreが持ちます。
会話外の活動の実行基盤だけを外に出し、その汎用Agentが使えるツールはCoreのMCPだけに限定します。
決まった定期処理（記憶の形成など）は、timerがCoreのAPIを呼びます（[ADR 0027](docs/adr/0027-tool-use-and-activity-runtime.md)。いずれも実装前）。
Coreは記憶の正本と検証、機微情報の流出防止ゲート、インジェクション対策を所有します。

記憶モデルはPoC（非公開`FYuki/digital-souls-poc`）で採用済みの決定を移設したものです
（[ADR 0015](docs/adr/0015-memory-model-reorganization.md)）。機能の実装状況と受入条件は[SPEC](SPEC.md)、
現行の記憶APIは[記憶API](docs/memory.md)、開発規約は[CONTRIBUTING](CONTRIBUTING.md)で管理します。
保存は既定拒否・明示注入のままで、私的な実会話を自動で取り込みません。
現行形式の分類・形成・検索・回答の実モデル品質は未受入です。
privacy分類器はgemma4-12b・合成60件をcacheなし3回評価し、各回51/60（85%）、
HEALTH・SELF_HARM・ABUSEの分類別基準未達と第3回の偽陰性2件によりFAILです。ABSTAINは各回0件です
（[分類器の実モデル証跡](docs/evidence/2026-10-12-privacy-classifier-real-model-evaluation.md)）。
正本へ合成登録した検索・回答の実モデル評価は上位5件包含の基準で各3回再実施済みで、いずれもFAILです
（[実モデル証跡](docs/evidence/2026-10-09-semantic-real-model-evaluation-top5.md)）。

最初の[キャラクター推論API](docs/api.md)をPython/FastAPI/LiteLLMで実装しています。
通常チャットと外部Agentのtool call往復に対応する小範囲のChat Completions APIです。
localhostの単独利用者向けで、公開サービス用の認証・tool実行は未実装です。
記憶永続化は明示的なopt-inで利用でき、既定では接続しません。
サンプルは外部送信を拒否し、fakeで無課金検証できます。

ローカルGemma 4を使う場合は[llama.cppの起動・切替・rollback](docs/llamacpp-operations.md)を参照してください。
固定Dockerと運用者profileを使用し、Ollamaや他クライアントの設定を自動変更しません。

[光織 / Miori](core/characters/miori/README.md)をサンプルキャラクターとして格納しています。
素材固有の利用条件と、保留・省略した項目のmanifestを確認してください。

## 明示的な会話履歴と記憶

既存APIはstatelessのままです。保存・復元・一覧・削除の専用経路と、既定拒否の保存policy境界を
[会話履歴API](docs/history-api.md)に記載しています。ローカル分類器、Episode・Fact・Semanticの正本登録・版・撤回、
正本からの意味検索とローカルembedding adapterは実装済みです。構造化抽出・保存判定・自動形成は未実装です。
旧逐語記憶・逐語抽出は撤去済みで、schema版6で旧3表を削除します。
embedding未接続時は記憶なしで会話を続けます。現行形式の実モデル品質は未受入です。
検索・回答の評価手順と制約は[意味検索・回答評価](docs/memory-evaluation.md)を参照してください。
bge-m3/0.52での[ローカルgemma4-12b評価](docs/evidence/2026-10-10-semantic-real-model-evaluation-bge-m3.md)に加え、
[GPT-6 Luna（Codexサブスク）の回答評価](docs/evidence/2026-10-12-semantic-luna-answer-evaluation.md)をcacheなし3回測定済みです。
回答は両モデルとも各回品質61/62・全条件53/62でFAIL、失敗ケースも一致し、品質は未受入です。

機微情報の明示的な検査・保存と送信の許可境界は[privacyガイド](docs/privacy.md)を参照してください。

現行の正本登録・参照検索と未実装・未検証範囲は[記憶API](docs/memory.md)を参照してください。

履歴・記憶の保存先は [PostgreSQL backend](docs/postgresql.md) へ一本化します（[ADR 0021](docs/adr/0021-postgresql-only-storage.md)）。
SQLite は撤去済みです。ローカルでもDockerのPostgreSQLを使います。
PostgreSQL は空の専用 schema を初期化し、履歴・記憶の既存契約を保ちます。
通常のアプリは保存無効のままで、データ移送は行いません。

PostgreSQL と意味検索を同時に使う trusted 起動側の構成・未実施の配備作業は
[統合と配備の境界](docs/semantic-postgresql-integration.md)を参照してください。
