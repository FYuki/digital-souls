# 開発環境

## 前提

開発作業は Linux / WSL2 上で行う。推奨作業ディレクトリは `~/dev/digital-souls` とし、`/mnt/c/Users/...` 配下は Node.js の依存関係処理やファイル監視が不安定になりやすいため避ける。

必要なランタイム:

| 種別 | 用途 | 起動方法 |
|---|---|---|
| Node.js | Frontend 開発サーバー | `scripts/start-frontend.sh` |
| Python 3 | FastAPI Backend | `scripts/setup-backend.sh` 後に `scripts/start-backend.sh` |
| Ollama | テキストチャットの LLM 推論 | `scripts/start-all.sh` または `scripts/start-ollama.sh` |
| Docker | VOICEVOX コンテナ実行 | `scripts/start-all.sh` または `scripts/start-voicevox.sh` |
| VOICEVOX | 音声チャットの TTS | `voicevox_engine` コンテナ |
| Whisper | 音声チャットの STT | Backend プロセス内で `faster-whisper` がロード |
| ChromaDB | 会話記憶のベクトルストア | Backend プロセス内の永続ストア |

PostgreSQL / Qdrant / Redis / AIRI は現行の通常起動フローでは使用しない。

## 初期セットアップ

```bash
sudo apt update
sudo apt install -y git curl build-essential docker.io python3 python3-venv

# Node.js（LTS）
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs

# Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Backend 仮想環境
scripts/setup-backend.sh
```

VOICEVOX は `voicevox_engine` という名前の既存コンテナを `docker start` で起動する。初回は次のように作成する。

```bash
docker run -d --name voicevox_engine -p 50021:50021 voicevox/voicevox_engine:cpu-latest
```

疎通確認:

```bash
curl http://localhost:11434/api/tags
curl http://localhost:50021/version
```

## 通常起動

環境全体の構成は `DS_PROFILE` で選択する。通常利用では次を実行する。

```bash
scripts/start-all.sh
```

`DS_PROFILE` 未指定時の `scripts/start-all.sh` は `dev` を選択する。別の構成を使う場合は起動前に明示する。

```bash
DS_PROFILE=integration-text scripts/start-all.sh
DS_PROFILE=test-mocked scripts/start-voice-chat-e2e.sh
```

初期 Profile は次の4種類である。各依存の完全な接続先と readiness path は `environments/profiles/*.json` を参照する。

| Profile | 用途 | 有効な依存 |
|---|---|---|
| `dev` | 通常のローカル開発 | Frontend、Backend、Ollama、VOICEVOX、Whisper |
| `test-mocked` | ブラウザ内 mock を使う独立 E2E | Frontend、browser mock Backend |
| `integration-text` | 実テキストチャット | Frontend、Backend、Ollama |
| `integration-voice` | 実音声チャット | Frontend、Backend、Ollama、VOICEVOX、Whisper |

起動スクリプトはサービス起動前に中央 resolver で Profile を検証し、既定では `frontend/test-results/resolved-profile.json` を生成する。この report には選択元、6依存の解決済み `mode` / `source` / 接続先、Capability、子プロセスへ渡す `derivedEnvironment` が記録される。`DS_PROFILE_REPORT` を指定すると出力先を変更できる。

`derivedEnvironment` の `OLLAMA_BASE_URL`、`VOICEVOX_BASE_URL`、`RAG_ENABLED`、`DS_BACKEND_ORIGIN` は resolver の解決結果から起動対象へ渡される。Backend は `backend/.env` の実行時設定も読み込むが、この4項目は読み込み後に resolved report の値を再適用するため、Profile の構成が優先される。

依存の `source` が `managed` の場合は対応するローカルプロセスまたはコンテナを起動して readiness を待つ。`external` の場合は起動せず、Profile の `readinessUrl` で外部サービスの準備完了だけを確認する。`disabled` の依存は起動しない。

`dev` では次の順序で起動確認を行う。

1. `scripts/setup-backend.sh` で Backend の仮想環境と依存関係を準備する
2. Ollama を起動し、`http://localhost:11434/api/tags` を確認する
3. VOICEVOX コンテナ `voicevox_engine` を起動し、`http://localhost:50021/version` を確認する
4. `scripts/start-backend.sh` で FastAPI Backend を起動し、`http://localhost:8000` を確認する
5. Frontend 開発サーバーを起動する

VOICEVOX コンテナが未作成の場合、`dev` または `integration-voice` の起動は Backend / Frontend を起動せず、初回セットアップ用の `docker run` 例を表示して終了する。

`VOICE_CHAT_E2E_BACKEND`、`CHAT_E2E_BACKEND`、`CHAT_E2E_BACKEND_ORIGIN`、`VOICE_CHAT_E2E_BACKEND_REPORT` は中央 resolver だけが解釈する非推奨の互換入口である。新しい起動・テスト設定では `DS_PROFILE` と `DS_PROFILE_REPORT` を使用する。`DS_PROFILE` と旧指定が異なる構成を示す場合や、複数の旧指定を単一 Profile に変換できない場合は、サービス起動前にエラーとなる。

## 個別起動スクリプト

| スクリプト | 役割 |
|---|---|
| `scripts/setup-backend.sh` | Backend の `.venv` を作成し、`backend/requirements.txt` をインストールする |
| `scripts/start-backend.sh` | `.venv` と `backend/.env` を読み、FastAPI を `uvicorn --reload` で起動する |
| `scripts/start-frontend.sh` | Frontend 開発サーバーを起動する |
| `scripts/start-ollama.sh` | `ollama serve` を起動する |
| `scripts/start-voicevox.sh` | Profile から渡された `VOICEVOX_BASE_URL` を使い、`voicevox_engine` コンテナを起動して `/version` を確認する。単体起動でも `backend/.env` 読み込み後に Profile の接続先を再適用する |
| `scripts/start-voice-chat-e2e.sh` | 音声チャット E2E 用。`DS_PROFILE` 未指定時は `integration-voice` を選択し、`test-mocked` では Frontend のみを起動する |

`scripts/start-backend.sh` は仮想環境の作成や依存インストールを自動実行しない。初回または依存関係の更新時は `scripts/setup-backend.sh` を別に実行する。セットアップ失敗は `Backend setup failed` と失敗工程、起動環境の不足は `start-backend.sh` の対象ファイル名を含むエラーで判別できる。Backend プロセスの起動後は、その終了ステータスが呼び出し元へ伝播する。

Backend 単体起動では Ollama や VOICEVOX を準備・起動しない。音声チャットを実際に使う場合は、事前に `scripts/start-voicevox.sh` または `scripts/start-all.sh` で VOICEVOX を起動する。開発用の `scripts/start-all.sh` と実 Backend を使う E2E 用の `scripts/start-voice-chat-e2e.sh` は、どちらも準備段階で `scripts/setup-backend.sh` を実行し、起動段階で共通の `scripts/start-backend.sh` を使う。

## 音声チャットの依存関係

音声チャットでは Backend の `AudioPipelineService` が STT、LLM、TTS を順に実行する。

- TTS は `VOICEVOX_BASE_URL` を参照し、未設定または空文字時は `http://localhost:50021` に接続する
- `VoicevoxClient` は `/audio_query` と `/synthesis` を呼び出す
- `scripts/start-voicevox.sh` のヘルスチェックは Profile から導出された `VOICEVOX_BASE_URL` に `/version` を付けて使用する。単体起動でも Profile の値が `backend/.env` より優先される
- Whisper は外部サービスではなく Backend プロセス内で `faster-whisper` の `WhisperModel("medium")` を初回利用時にロードする
- Whisper モデルは初回利用時に取得が発生し得るため、オフライン環境では事前にモデルキャッシュを用意する

## ChromaDB

ChromaDB は外部プロセスではなく、Backend プロセス内で `chromadb.PersistentClient` として利用する。永続化先は `backend/app/data/chroma` で、初回利用時に `backend/app/data` が作成される。

リポジトリ配下に永続データが作られるため、開発環境では作業ユーザーが `backend/app/data` を作成・書き込みできる権限を持っている必要がある。

## テストとの関係

テスト層と外部サービス実接続の扱いは `docs/testing-policy.md` を参照する。VOICEVOX / Whisper / ChromaDB / Ollama の実接続を完了条件として報告する場合は、同ドキュメントのインテグレーションテスト方針に従い、実サービスへの接続ログを一次証跡にする。
