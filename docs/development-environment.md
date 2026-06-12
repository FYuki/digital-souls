# 開発環境

## 初期開発環境

Mac mini調達までは、WindowsメインPC上のWSL2で開発を進める。

```text
Windows
├─ VSCode
├─ WSL2 Ubuntu
├─ Docker Desktop
├─ Git
├─ Node.js
├─ Python
└─ Ollama
```

## WSL2利用方針

開発作業は基本的にWSL2内で行う。

推奨作業ディレクトリ:

```bash
~/projects/digital-souls
```

避ける構成:

```bash
/mnt/c/Users/...
```

理由:

* Node.jsの依存関係処理が遅くなりやすい
* Dockerやファイル監視で問題が出やすい
* Linux/Mac環境への移行性が下がる

## Ollama利用方針

初期はWindows側のOllamaを利用し、WSL2からAPI経由でアクセスする構成を優先する。

```text
WSL2
└─ digital-souls / AIRI

Windows
└─ Ollama
```

WSL2内でOllamaを直接動かす構成も可能だが、初期段階ではトラブルを減らすためWindows側Ollamaを優先する。

## Docker利用方針

将来的に以下のサービスをDocker Composeで管理する。

候補:

* PostgreSQL
* Qdrant
* Redis
* アプリケーション本体
* 管理用Web UI

初期段階では、まずドキュメントと人格設計を優先し、Docker構成は後続タスクとする。

## Mac mini移行方針

Mac mini導入後は、以下の流れで移行する。

```bash
git clone https://github.com/FYuki/digital-souls.git
cd digital-souls
docker compose up -d
```

実際の移行時には、以下を整理する。

* 環境変数
* モデル保存先
* DBバックアップ
* Qdrantデータ移行
* 起動スクリプト
* 自動起動設定
* ログ管理

## 初期セットアップ候補

```bash
sudo apt update
sudo apt install -y git curl build-essential

# Node.js / Python / Docker は採用技術決定後に確定する
```

## 今後の検証項目

* WSL2でAIRIが問題なく動作するか
* Ollama + Gemma 4B級の応答速度
* PostgreSQL / Qdrant / Redisの必要性
* Windows側Ollamaへの接続方法
* Mac mini移行時の差分
