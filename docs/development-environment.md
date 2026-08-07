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

## dogfood環境との境界

本書の既存コマンドは`Ubuntu-dev`上の開発・テスト環境を対象とする。継続利用するdogfoodは
Issue #50で別WSL distribution、別port、独立clone、専用data rootへ分離する。

| 環境 | Frontend | Backend | ready gate | データ |
|---|---:|---:|---:|---|
| dev／TAKT | 5173 | 8000 | 4174 | 破棄・再作成可能 |
| dogfood | 15173 | 18000 | 14174 | backup・migration対象 |

dogfoodは専用Profileと操作入口を使用し、`dev` Profileやmain checkoutをdogfood用途へ流用しない。
dev／testのsetup、fixture、cleanupからdogfood data rootを指定しない。

Wave 2親Issue #28の受入まではdogfoodのRAGを無効にし、実データとして保持するのは
conversation historyだけとする。詳細は
`docs/decisions/local-dogfood-environment-2026-08.md`を参照する。

## runtime data root

`DS_ENVIRONMENT_ID`は`dev`、`test`、`dogfood`の実行環境を識別し、未指定時は`dev`となる。
`DS_DATA_DIR`は絶対パスで指定する単一のdata rootで、未指定のdevでは従来どおり
`backend/app/data`を使用する。

| 対象 | data rootからの相対パス |
|---|---|
| 環境identity marker | `.environment-identity.json` |
| conversation history SQLite | `conversation-history.db` |
| Chroma | `chroma/` |
| runtime report | `runtime/` |
| cache | `cache/`（Whisperは`cache/huggingface/hub/`） |

起動時はmarkerを作成または検証し、環境IDとの不一致、markerの欠落・破損、相対パス、symlink、
通常ファイル、書き込み不能な場所、危険な広域パスをSQLite／Chromaの初期化前に拒否する。
dogfoodではリポジトリ外の専用絶対パスが必須である。

```bash
export DS_ENVIRONMENT_ID=dogfood
export DS_DATA_DIR=/var/lib/digital-souls/dogfood
```

dogfoodの操作入口は`DS_PROFILE`と`DS_ENVIRONMENT_ID`を`dogfood`へ固定し、同じdata root内の
所有reportをstart／stop／statusで共有する。

```bash
export DS_DATA_DIR=/var/lib/digital-souls/dogfood
scripts/start-dogfood.sh
scripts/status-dogfood.sh
scripts/stop-dogfood.sh
```

dogfood Frontend／Backend／ready gateはそれぞれ15173／18000／14174を使うため、
5173／8000／4174を使うdev・integration Profileと同時起動できる。dogfoodのOllamaと
VOICEVOXは`external`であり、dogfood runの所有対象にも`stop`の対象にもならない。
Chroma／RAGはWave 2受入まで無効で、起動・probe・所有を行わない。

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

Profile は次の5種類である。各依存の完全な接続先と readiness path は `environments/profiles/*.json` を参照する。

| Profile | 用途 | 有効な依存 |
|---|---|---|
| `dev` | 通常のローカル開発 | Frontend、Backend、Ollama、VOICEVOX、Whisper |
| `test-mocked` | ブラウザ内 mock を使う独立 E2E | Frontend、browser mock Backend |
| `integration-text` | 実テキストチャット | Frontend、Backend、Ollama |
| `integration-voice` | 実音声チャット | Frontend、Backend、Ollama、VOICEVOX、Whisper |
| `dogfood` | 継続利用する運用相当環境 | Frontend、Backend、external Ollama／VOICEVOX、Whisper |

起動スクリプトはサービス起動前に中央 resolver で Profile を検証する。runtime reportとresolved Profileは解決済みdata rootの`runtime/`配下にのみ保存する。Playwrightは各スイート専用のtest data rootを設定し、`runtime/standalone/`へ環境reportを、`frontend/test-results/<suite>/`へテスト証跡を保存する。reportには環境IDと正規化済みpathを記録し、秘密値や会話本文は記録しない。

`derivedEnvironment` の接続先に加え、`OLLAMA_CHAT_MODEL`、`WHISPER_MODEL`、`OLLAMA_CONTEXT_TOKENS`、応答予約量、履歴・入力・モデルcontext上限は resolver の解決結果から起動対象へ渡される。`scripts/start-backend.sh` は `backend/.env` をProfile解決前に読み込む。`DS_PROFILE_REPORT`で既存のresolved reportを指定しない場合は`.env`のモデル設定を解決結果へ取り込み、指定した場合はreportの解決済み設定を優先する。不正な文字列、正でない整数、応答予約量が実行時context以上、または実行時contextがモデル最大contextを超える指定は、サービス起動前に拒否される。

依存の `source` が `managed` の場合は対応するローカルプロセスまたはコンテナを起動して readiness を待つ。`external` の場合は起動せず、Profile の `readinessUrl` で外部サービスの準備完了だけを確認する。`disabled` の依存は起動しない。

`dev` では次の順序で起動確認を行う。

1. `scripts/setup-backend.sh` で Backend の仮想環境と依存関係を準備する
2. Ollama を起動し、`http://localhost:11434/api/tags` を確認する
3. VOICEVOX コンテナ `voicevox_engine` を起動し、`http://localhost:50021/version` を確認する
4. managed adapterが`start-backend.sh --host localhost --port 8000 --reload`で FastAPI Backend を起動し、`http://localhost:8000` を確認する
5. Frontend 開発サーバーを起動する

VOICEVOX コンテナが未作成の場合、`dev` または `integration-voice` の起動は Backend / Frontend を起動せず、初回セットアップ用の `docker run` 例を表示して終了する。

`VOICE_CHAT_E2E_BACKEND`、`CHAT_E2E_BACKEND`、`CHAT_E2E_BACKEND_ORIGIN`、`VOICE_CHAT_E2E_BACKEND_REPORT` は中央 resolver だけが解釈する非推奨の互換入口である。新しい起動・テスト設定では `DS_PROFILE` と `DS_PROFILE_REPORT` を使用する。`DS_PROFILE` と旧指定が異なる構成を示す場合や、複数の旧指定を単一 Profile に変換できない場合は、サービス起動前にエラーとなる。

## 個別起動スクリプト

| スクリプト | 役割 |
|---|---|
| `scripts/setup-backend.sh` | Backend の `.venv` を作成し、`backend/requirements.txt` をインストールする |
| `scripts/start-backend.sh` | resolved Profile由来の明示的な`--host`、`--port`、任意の`--reload`を受け、FastAPIを起動する |
| `scripts/start-frontend.sh` | Frontend 開発サーバーを起動する |
| `scripts/start-ollama.sh` | `ollama serve` を起動する |
| `scripts/start-voicevox.sh` | `dev` Profile の VOICEVOX adapter だけを起動する単体入口 |
| `scripts/start-voice-chat-e2e.sh` | 音声チャット E2E 用。`DS_PROFILE` 未指定時は `integration-voice` を選択し、`test-mocked` では Frontend のみを起動する |
| `scripts/start-dogfood.sh` | dogfood Profileとidentityを固定して起動する |
| `scripts/status-dogfood.sh` | dogfoodのowned managedとunowned externalを区別して表示する |
| `scripts/stop-dogfood.sh` | dogfoodの所有reportに記録されたmanagedプロセスだけを停止する |

`scripts/start-backend.sh` は仮想環境の作成や依存インストールを自動実行しない。初回または依存関係の更新時は `scripts/setup-backend.sh` を別に実行する。セットアップ失敗は `Backend setup failed` と失敗工程、起動環境の不足は `start-backend.sh` の対象ファイル名を含むエラーで判別できる。Backend プロセスの起動後は、その終了ステータスが呼び出し元へ伝播する。

Backend 単体起動では Ollama や VOICEVOX を準備・起動しない。VOICEVOX だけを起動する場合は `scripts/start-voicevox.sh`、音声チャットの全依存を起動する場合は `scripts/start-all.sh` を使う。開発用の `scripts/start-all.sh` と実 Backend を使う E2E 用の `scripts/start-voice-chat-e2e.sh` は、どちらも準備段階で `scripts/setup-backend.sh` を実行し、起動段階で共通の `scripts/start-backend.sh` を使う。

## 音声チャットの依存関係

音声チャットでは Backend の `AudioPipelineService` が STT、LLM、TTS を順に実行する。

- TTS は `VOICEVOX_BASE_URL` を参照し、未設定または空文字時は `http://localhost:50021` に接続する
- `VoicevoxClient` は `/audio_query` と `/synthesis` を呼び出す
- 共通環境オーケストレーターの VOICEVOX adapter は Profile の `readinessUrl` で `/version` を確認する
- Whisper は外部サービスではなく Backend プロセス内で `WHISPER_MODEL`（既定 `medium`）を初回利用時にロードする
- 共通環境オーケストレーターは prepare で Whisper モデルを`<data root>/cache/huggingface/hub`へ準備し、Backend 実行時も同じ保存先を使う
- `<data root>/cache/huggingface/` は Git 管理対象外である。Backend を単体起動する場合は初回利用時に取得が発生し得るため、オフライン環境では事前にこのキャッシュを用意する
- `WHISPER_MODEL` を変更した場合、prepare時のcache名・ダウンロード対象・Backend実行モデルが一緒に切り替わる

## ChromaDB

ChromaDB は外部プロセスではなく、Backend プロセス内で `chromadb.PersistentClient` として利用する。永続化先は解決済みdata rootの`chroma/`である。

リポジトリ配下に永続データが作られるため、開発環境では作業ユーザーが `backend/app/data` を作成・書き込みできる権限を持っている必要がある。

dogfood ChromaはWave 2受入後にSQLite正本から構築する。

## テストとの関係

テスト層と外部サービス実接続の扱いは `docs/testing-policy.md` を参照する。VOICEVOX / Whisper / ChromaDB / Ollama の実接続を完了条件として報告する場合は、同ドキュメントのインテグレーションテスト方針に従い、実サービスへの接続ログを一次証跡にする。
