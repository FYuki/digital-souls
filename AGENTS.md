## 現行仕様の確認

実装の入口は[README](README.md)、用語は[用語集](CONTEXT.md)、現在の責務境界は
[システムアーキテクチャ](docs/system-architecture.md)を参照する。
設計判断の優先関係は[ADR案内](docs/decisions/README.md)、具体的な進捗・依存・完了条件はGitHub Issuesで管理する。

ADRの`ACTIVE`は判断の有効性だけを表し、実装・既定有効・受入の状況を表さない。
実装状態を説明・変更するときは対象ブランチのコードと設定、進捗はIssueを照合する。用語や責務境界を追加・変更する場合は、
対応ADRに加えて`CONTEXT.md`の定義・実装名・状態・参照先を更新する。
文書の正本と書き方は[文書の正本と更新責務](docs/decisions/documentation-governance-2026-09.md)に従い、
同じ事実を複数文書へ複製しない。ADRへ進捗・受入状況・作業ブランチ名を書かない。

## 技術スタック

各層の技術と責務は[システムアーキテクチャ](docs/system-architecture.md)を正本とする。作業時は次の制約を守る。

- `privacy`と`character-life`のローカルProvider制約は設定で緩和しない。推論設定は[推論運用](docs/inference-operations.md)を確認する。
- 会話履歴・長期記憶はSQLiteを正本、Chromaを派生indexとする。`character_id`で所有を分離する。
- GPUモデル（Whisper等）は共有サービスが所有し、Backendのプロセスへ読み込まない。
- 既存の非同期形成経路の許可型（`EPISODIC_EVENT / USER_PREFERENCE / INTERACTION_PREFERENCE`）と、
  SELF経験・意味抽象化・独立した内省記憶・人格適応を混同しない。

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

ディレクトリ構成は[README](README.md#ディレクトリ構成)、格納方針は[リポジトリ運用方針](docs/repository-policy.md)を参照する。

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
