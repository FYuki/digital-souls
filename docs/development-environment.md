# 開発環境

> **注記**: 本ドキュメントは旧構成（PostgreSQL/Qdrant/Redis/AIRI）の記述を含み陳腐化している。
> 現行構成への改訂は issue #32 で対応予定。

## 初期開発環境

Mac mini調達までは、WindowsメインPC上のWSL2で開発を進める。

```text
Windows
└─ VSCode

WSL2 Ubuntu（LTS最新安定版）
├─ Git
├─ Node.js（LTS最新安定版）
├─ Ollama
├─ PostgreSQL
├─ Qdrant
├─ Redis
└─ AIRI
```

## WSL2利用方針

開発作業は基本的にWSL2内で行う。

推奨作業ディレクトリ:

```bash
~/dev/digital-souls
```

避ける構成:

```bash
/mnt/c/Users/...
```

理由:

- Node.jsの依存関係処理が遅くなりやすい
- ファイル監視で問題が出やすい
- Linux/Mac環境への移行性が下がる

## Ollama利用方針

WSL2内にOllamaを直接インストールして使用する。

疎通確認:

```bash
curl http://localhost:11434/api/tags
```

## サービス構成

以下をWSL2上に直接インストールして管理する。

| サービス | インストール方法 | 用途 |
|---|---|---|
| PostgreSQL | apt install | 記憶DB・農業日誌・レシピ管理 |
| Qdrant | バイナリ直接起動 | ベクトル検索（RAG） |
| Redis | apt install | キャッシュ |
| Ollama | 公式インストーラ | LLM推論 |
| AIRI | WSL2直接起動 | 人格・エージェント制御 |

## 環境分け

開発・本番は環境変数ファイルで分離する。

| ファイル | 用途 | リポジトリ管理 |
|---|---|---|
| `.env.development` | WSL2ローカル開発 | 除外（.gitignore） |
| `.env.production` | Mac mini本番 | 除外（.gitignore） |
| `.env.example` | 設定項目のテンプレート | 含める |

DBはPostgreSQL内でデータベース名を分けて管理する。

| 環境 | DB名 |
|---|---|
| 開発 | digital_souls_dev |
| 本番 | digital_souls |

## Mac mini移行方針

Mac mini導入後は以下の手順で移行する。

```bash
git clone https://github.com/FYuki/digital-souls.git
cd digital-souls
# 各サービスをインストール・起動
# .env.production を配置
```

移行時に整理する項目:

- 環境変数の移植
- モデル保存先（Ollama）
- DBデータのバックアップ・リストア
- Qdrantデータの移行
- 自動起動設定（launchd）
- ログ管理

## 初期セットアップ（Linux / WSL2）

以下の手順は **Linux（WSL2 Ubuntu）専用**。Mac mini移行時の手順は Phase 6 で整備する。

```bash
sudo apt update
sudo apt install -y git curl build-essential

# Node.js（LTS最新安定版）
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs

# PostgreSQL
sudo apt install -y postgresql postgresql-contrib

# Redis
sudo apt install -y redis-server

# Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Qdrant（バイナリ）
# インストール手順はPhase 2検証時に確定する
```

## Mac環境構築に関する注意点（Phase 6 向け先行メモ）

Mac mini移行時に Claude Code で環境構築する際は、以下の点に注意する。
詳細手順は Phase 6 で整備する。

| 項目 | Linux（WSL2） | Mac |
|---|---|---|
| パッケージマネージャ | `apt` | `brew`（Homebrew が前提） |
| PostgreSQL | `apt install postgresql` | `brew install postgresql@17` 等 |
| Redis | `apt install redis-server` | `brew install redis` |
| Node.js | nodesource スクリプト | `nvm` または `brew install node` |
| Qdrant | Linux用バイナリ | macOS用バイナリ（URL・パスが異なる） |
| 自動起動 | systemd | launchd（設定例は Phase 6 で追加） |

- `sudo apt install` をそのまま実行すると `apt: command not found` で失敗する
- Homebrew 未インストールの場合はほぼすべての手順が動かないため、先にインストールが必要
- WSL2固有の注意事項（`/mnt/c/...` を避ける等）は Mac には無関係
