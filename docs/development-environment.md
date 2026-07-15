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

通常利用では次を実行する。

```bash
scripts/start-all.sh
```

`scripts/start-all.sh` は次の順序で起動確認を行う。

1. `scripts/setup-backend.sh` で Backend の仮想環境と依存関係を準備する
2. Ollama を起動し、`http://localhost:11434/api/tags` を確認する
3. VOICEVOX を起動確認する。`VOICEVOX_BASE_URL` 未設定または空文字時はコンテナ `voicevox_engine` を起動し、`http://localhost:50021/version` を確認する
4. `scripts/start-backend.sh` で FastAPI Backend を起動し、`http://localhost:8000` を確認する
5. Frontend 開発サーバーを起動する

`backend/.env` で `VOICEVOX_BASE_URL` を別ホスト・別ポートに設定している場合、`scripts/start-all.sh` はその接続先の `/version` を確認し、ローカルの `voicevox_engine` コンテナは起動しない。`http://127.0.0.1:50021` は `http://localhost:50021` と同じローカル既定エンジンとして扱う。

VOICEVOX コンテナが未作成の場合、`VOICEVOX_BASE_URL` 未設定または空文字時の `scripts/start-all.sh` は Backend / Frontend を起動せず、初回セットアップ用の `docker run` 例を表示して終了する。

## 個別起動スクリプト

| スクリプト | 役割 |
|---|---|
| `scripts/setup-backend.sh` | Backend の `.venv` を作成し、`backend/requirements.txt` をインストールする |
| `scripts/start-backend.sh` | `.venv` と `backend/.env` を読み、FastAPI を `uvicorn --reload` で起動する |
| `scripts/start-frontend.sh` | Frontend 開発サーバーを起動する |
| `scripts/start-ollama.sh` | `ollama serve` を起動する |
| `scripts/start-voicevox.sh` | `backend/.env` の `VOICEVOX_BASE_URL` を読み、未設定または空文字時は `voicevox_engine` コンテナを起動して VOICEVOX の `/version` を確認する |
| `scripts/start-voice-chat-e2e.sh` | 音声チャット E2E 用。既定では実 Backend / Ollama / VOICEVOX を起動し、`VOICE_CHAT_E2E_BACKEND=mock` では Frontend のみ起動する |

`scripts/start-backend.sh` は仮想環境の作成や依存インストールを自動実行しない。初回または依存関係の更新時は `scripts/setup-backend.sh` を別に実行する。セットアップ失敗は `Backend setup failed` と失敗工程、起動環境の不足は `start-backend.sh` の対象ファイル名を含むエラーで判別できる。Backend プロセスの起動後は、その終了ステータスが呼び出し元へ伝播する。

Backend 単体起動では Ollama や VOICEVOX を準備・起動しない。音声チャットを実際に使う場合は、事前に `scripts/start-voicevox.sh` または `scripts/start-all.sh` で VOICEVOX を起動する。開発用の `scripts/start-all.sh` と実 Backend を使う E2E 用の `scripts/start-voice-chat-e2e.sh` は、どちらも準備段階で `scripts/setup-backend.sh` を実行し、起動段階で共通の `scripts/start-backend.sh` を使う。

## 音声チャットの依存関係

音声チャットでは Backend の `AudioPipelineService` が STT、LLM、TTS を順に実行する。

- TTS は `VOICEVOX_BASE_URL` を参照し、未設定または空文字時は `http://localhost:50021` に接続する
- `VoicevoxClient` は `/audio_query` と `/synthesis` を呼び出す
- `scripts/start-voicevox.sh` のヘルスチェックは Backend と同じ `backend/.env` の `VOICEVOX_BASE_URL` 解決結果に `/version` を付けて使用する
- Whisper は外部サービスではなく Backend プロセス内で `faster-whisper` の `WhisperModel("medium")` を初回利用時にロードする
- Whisper モデルは初回利用時に取得が発生し得るため、オフライン環境では事前にモデルキャッシュを用意する

## ChromaDB

ChromaDB は外部プロセスではなく、Backend プロセス内で `chromadb.PersistentClient` として利用する。永続化先は `backend/app/data/chroma` で、初回利用時に `backend/app/data` が作成される。

リポジトリ配下に永続データが作られるため、開発環境では作業ユーザーが `backend/app/data` を作成・書き込みできる権限を持っている必要がある。

## テストとの関係

テスト層と外部サービス実接続の扱いは `docs/testing-policy.md` を参照する。VOICEVOX / Whisper / ChromaDB / Ollama の実接続を完了条件として報告する場合は、同ドキュメントのインテグレーションテスト方針に従い、実サービスへの接続ログを一次証跡にする。
