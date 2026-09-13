## 現行仕様の確認

実装の入口は[README](README.md)、用語は[用語集](docs/glossary.md)、現在の責務境界は
[システムアーキテクチャ](docs/system-architecture.md)を参照する。
設計判断の優先関係は[ADR案内](docs/decisions/README.md)、具体的な進捗・依存・完了条件はGitHub Issuesで管理する。

ADRが`ACTIVE`でも、その全体が実装済み・既定有効・dogfood受入済みとは限らない。
実装状態を説明・変更するときは対象ブランチのコードと設定を照合する。用語や責務境界を追加・変更する場合は、
対応ADRに加えて`docs/glossary.md`の定義・実装名・状態・参照先を更新する。

## 技術スタック

2026-06-17にAIRIフォークから自作BE／FEへ移行した。以下は現行の実装である。

| 層 | 技術・責務 |
|---|---|
| バックエンド | FastAPI（Python）。会話、キャラクター、記憶、推論、画面知覚、MCP、管理API |
| フロントエンド | Vite + Svelte + TypeScript。テキスト／音声会話、スレッド、静止画立ち絵、管理UI |
| 推論 | `inference/`の用途別TargetとProvider Adapter。Ollama／OpenAI API／Codex runtimeを環境設定で選ぶ。暗黙fallbackはない |
| 会話履歴・長期記憶 | SQLiteを正本、Chromaを承認済み長期記憶の派生indexとする。`character_id`で所有を分離し、履歴はさらに`conversation_id`で分離 |
| 音声通信 | LiveKitが正式経路。Speech/Text共通のConversation Sessionを使用。旧WebSocketは移行前baseline／互換用 |
| STT | 共有GPU Whisper HTTP service。Backendはremote clientを使い、GPUモデルを所有しない |
| TTS | VOICEVOX。応答単位の逐次合成・再生・cancelと完了確認 |
| 外部連携 | 登録済みMCP、Capability Snapshot、Execution Gate、Tool routing、操作承認・結果回復、Addon管理 |
| 会話外活動 | DBOS + Character Life。既定無効・dev/test対象。記憶・内省・人格・Skillとの全体接続は未完了 |
| 通常起動 | Environment Profile + Docker。サービス所有とruntime data rootを環境ごとに分離 |

Ollamaの開発用モデル等は`backend/.env.example`と[推論運用](docs/inference-operations.md)を確認する。
`privacy`と`character-life`のローカルProvider制約は設定で緩和しない。
既存の非同期形成経路の許可型は`EPISODIC_EVENT / USER_PREFERENCE / INTERACTION_PREFERENCE`であり、
SELF経験、意味抽象化、独立した内省記憶、人格適応を実装済みの記憶統合と混同しない。

Episode / Factの保存・登録・参照・管理基盤へ、会話スレッド単位の永続予約と非同期抽出workerを接続する。
通常抽出は反復18を採用したv13-compact18を使う。実装・固定評価と、devでの総合受入を区別する。

Live2D、VRM、Desktop／Discord等の追加クライアント、複数キャラクターのグループ会話、
Mac mini等への常時稼働環境移行は拡張方針である。現行のブラウザ・静止画UIと区別する。

## 環境

- Windows + WSL2を使用し、Linux／WSL2側で開発する。
- `Ubuntu-dev`は開発・検証用で、データを破棄可能とする。
- `Ubuntu-dogfood`は実会話履歴を扱う運用相当環境。独立clone、専用port、リポジトリ外data rootを使う。
- 共有Ollama／VOICEVOX／Whisperはdogfood側のservice管理が所有する。dev用LiveKitは専用手順で用意する。
- dev/testのsetup、fixture、cleanupからdogfoodのデータを操作しない。

標準の起動・停止は[開発環境](docs/development-environment.md)、dogfoodの配備・backup・restoreは
[専用手順](infra/dogfood/README.md)に従う。`scripts/start-all.sh`とEnvironment CLIを標準入口とし、
個別の`start-backend.sh`／`start-frontend.sh`は通常のDocker起動と区別した互換・単体開発用経路として扱う。
`main`へのマージだけではdogfoodへ自動deployしない。

`DS_PROFILE`はサービス構成、`DS_ENVIRONMENT_ID`は`dev / test / dogfood`というデータ識別、
`DS_DATA_DIR`はruntime data rootである。Profile名と環境IDを同一視しない。

## 規約

- ドキュメントはすべて日本語で記述する。コードのコメントも日本語を基本とする。
- コミットメッセージはConventional Commits形式（英語可）とする。
- runtimeのキャラクター定義は`characters/{id}/{id}.card.json`を正本とし、`personality.md`から合成しない。
- 検証の実行結果と未実行理由を分けて記録する。モックE2Eを実サービス・実マイクの受入証跡にしない。
- 秘密値、会話原文、外部サービスのnative payloadをIssueやログへ転記しない。
- TAKT上のチャットでは対話的な質問ツールが使用できないため、確認事項はテキストで表示する。

テストの層分け・準備は[テスト方針](docs/testing-policy.md)、実行可能なコマンドは
[package.json](package.json)を確認する。ブランチ運用と文書責務は[リポジトリ運用方針](docs/repository-policy.md)に従う。

## リポジトリ構成

```text
digital-souls/
├─ backend/          # FastAPI・会話・記憶・推論・外部連携・活動
├─ frontend/         # ブラウザUI・LiveKit client
├─ characters/       # Character Cardと編集用補助資料
├─ contracts/        # FE/BE共有契約
├─ environments/     # Profile・環境管理
├─ infra/            # Compose・dogfood運用
├─ whisper_service/  # 共有Whisper HTTP service
├─ scripts/          # 起動・検証・運用入口
└─ docs/             # 用語集・構成・ADR・運用・受入証跡
```

## 実装フロー管理（TAKT）

実装フローはTAKTで管理する。

```bash
# インストール（初回のみ）
npm install -g takt

# タスクを相談して積む
takt

# GitHub Issueを積む（#を含む場合はクオート必須）
takt add "#9"

# 実行と結果確認
takt run
takt list
```

| ワークフロー | 用途 |
|---|---|
| `default`（ビルトイン） | 標準開発（計画→実装→レビュー） |
| `default-mini`（ビルトイン） | 小規模な変更・ホットフィックス |

必要なプロジェクト設定・カスタムワークフローは`.takt/`で扱う。
`tasks/`・`logs/`は`.takt/.gitignore`で除外し、ローカル管理とする。
