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
│     ├─ personality.md
│     ├─ world.md
│     └─ memory-policy.md
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

## characters

`characters/` には人格ごとの設定を格納する。

人格ごとにディレクトリを分ける。

```text
characters/
├─ miori/
├─ another-persona/
└─ common/
```

想定ファイル:

* `personality.md`
* `world.md`
* `memory-policy.md`
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
