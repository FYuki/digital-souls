# リポジトリ運用方針

## 基本方針

`digital-souls` は、複数のAI人格とその実行基盤を管理するリポジトリとして扱う。

光織専用リポジトリにはせず、将来的に複数人格を追加できる構成にする。

## 格納方針

```text
repo/
├─ docs/
│  ├─ roadmap.md
│  ├─ system-architecture.md
│  ├─ infrastructure-policy.md
│  ├─ development-environment.md
│  ├─ repository-policy.md
│  └─ decisions/
│     └─ <topic>-YYYY-MM.md
│
├─ characters/
│  └─ miori/
│     ├─ miori.card.json
│     ├─ personality.md  # Character Card編集用の非runtime補助資料
│     ├─ world.md
│     └─ memory-policy.md  # 方針本文と実装設定への案内
│
└─ src/
   └─ 将来の実装コード
```

## docs

`docs/` には、設計・運用・方針を格納する。

対象:

* 開発ロードマップ
* システムアーキテクチャ
* インフラ方針
* 開発環境
* リポジトリ運用方針
* 技術選定メモ

### 文書ごとの責務

同じ内容を複数の文書へ詳細に複製せず、次の責務で分ける。

| 文書 | 責務 |
|---|---|
| `roadmap.md` | Phase／Wave単位の目標と進捗を簡潔に示す。実装条件やschema versionは書かず、詳細文書とIssueを参照する |
| `enhancement-plan.md` | 機能の分解、依存関係、実施順序、完了イメージを示す |
| `system-architecture.md` | 現在のコードで実現している構成、責務境界、runtimeの挙動を現在形で示す |
| `decisions/` | 判断の背景、選択肢、決定事項、不変条件を記録する。タスクの進捗は管理しない |
| GitHub Issues | 具体的な実装・文書作業の範囲、依存、テスト、完了条件を管理する。完了後は当時の実装契約の記録として扱う |
| 運用手順書 | backup、migration、rollback、障害対応等の実行手順を扱う。実運用開始時に必要な文書を追加する |

schemaやデータ保持の方針はADR、現在実装されているschemaとruntime挙動は
`system-architecture.md`、対応作業はIssueへ記載する。`roadmap.md`には完了状態と参照先だけを残す。

## characters

`characters/` には人格ごとの設定を格納する。

人格ごとにディレクトリを分ける。

```text
characters/
├─ miori/
├─ another-persona/
└─ common/
```

基本ファイル:

* `{id}.card.json`（runtimeで使用する人格・表示名・会話例・応答指示の正本）
* `personality.md`（Character Card編集用の非runtime補助資料）
* `world.md`
* `memory-policy.md`（方針本文と実装設定への案内）

runtimeは`{id}.card.json`を直接読み込み、`personality.md`から人格情報を合成しない。

追加可能な補助ファイル:

* `voice.md`
* `appearance.md`

## docs/decisions

`docs/decisions/` には、検討経緯と意思決定ログを残す。

ファイル命名規則: `<topic>-YYYY-MM.md`

想定ファイル例:

* `airi-2026-06.md` — AIRI採用方針
* `character-miori-2026-06.md` — 光織キャラクター設定補正
* `infrastructure-2026-07.md` — インフラ構成選定

## GitHub Issues

Issuesは、実装またはドキュメント作業を具体的なタスクに切り出した後に使用する。

初期段階では、議論をDiscussionsに残し、実行可能な作業になったものをIssue化する。

Issue例:

* `[docs] システムアーキテクチャを整理する`
* `[infra] WSL2開発環境を構築する`
* `[infra] Ollama + Gemma 4Bを検証する`
* `[design] 光織の人格設計を作成する`
* `[feature] 農業日誌ツールを設計する`
* `[feature] レシピ管理ツールを設計する`

## GitHub Projects

個人開発のため、当面はGitHub Projectsを使用しない。

必要になった場合のみ、Issuesをまとめるために導入する。

## テスト証跡

テスト件数・通過数をレポートに記載する場合は、実行ログを一次証跡として扱う。

`mypy` などのツールが出力する対象ファイル件数は、ソース追加で変動するため契約テストに固定しない。レポートでは成功可否を記録し、可変件数を仕様として扱わない。

テストの層分け（ユニット/インテグレーション/E2E）、外部サービス（Ollama/ChromaDB/VOICEVOX/Whisper）への実接続要件、命名規則は `docs/testing-policy.md` を参照する。外部サービスとの実連携を完了条件として報告する場合は、同ドキュメントが定めるインテグレーションテストの実行ログを一次証跡とする。

## ブランチ運用

基本は以下の方針とする。

* `main`: 安定版
* `docs/*`: ドキュメント作業
* `feature/*`: 機能追加
* `infra/*`: インフラ・環境構築
* `character/*`: 人格設計

初期作業ブランチ:

```text
docs/init-digital-souls
```
