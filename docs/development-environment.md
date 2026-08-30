# 開発環境

## 前提

開発作業は Linux / WSL2 上で行う。推奨作業ディレクトリは `~/dev/digital-souls` とし、`/mnt/c/Users/...` 配下は Node.js の依存関係処理やファイル監視が不安定になりやすいため避ける。

必要なランタイム:

| 種別 | 用途 | 起動方法 |
|---|---|---|
| Node.js | Frontend 開発サーバー | `scripts/start-frontend.sh` |
| Python 3 | FastAPI Backend | `scripts/setup-backend.sh` 後に `scripts/start-backend.sh` |
| Ollama | テキストチャットの LLM 推論 | Ubuntu-dogfoodのsystemdが所有。dev／integrationは起動済みendpointを再利用 |
| Docker | VOICEVOX／LiveKit コンテナ実行 | Ubuntu-dogfoodのsystemdがCompose stackを操作し、Composeが実行中containerを所有 |
| VOICEVOX | 音声チャットの TTS | `voicevox_engine` コンテナ |
| LiveKit | Wave 3音声transport | dogfoodは`digital-souls-livekit.service`、dev integrationは`infra/livekit/compose.yaml` |
| Whisper | 音声チャットの STT | Backend プロセス内で `faster-whisper` がロード |
| ChromaDB | 会話記憶のベクトルストア | Backend プロセス内の永続ストア |

PostgreSQL / Qdrant / Redis / AIRI は現行の通常起動フローでは使用しない。

## dogfood環境との境界

本書の既存コマンドは`Ubuntu-dev`上の開発・テスト環境を対象とする。継続利用するdogfoodは
Issue #50で別WSL distribution、別port、独立clone、専用data rootへ分離する。

| 環境 | Frontend | Backend | ready gate | LiveKit | データ |
|---|---:|---:|---:|---:|---|
| dev／TAKT | 5173 | 8000 | 4174 | 7880 | 破棄・再作成可能 |
| dogfood | 15173 | 18000 | 14174 | 17880 | backup・migration対象 |

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
export DS_DATA_DIR=/var/lib/digital-souls/data
```

dogfoodの操作入口は`DS_PROFILE`と`DS_ENVIRONMENT_ID`を`dogfood`へ固定し、同じdata root内の
所有reportをstart／stop／statusで共有する。

```bash
export DOGFOOD_ENV_FILE=/etc/digital-souls/dogfood.env
scripts/start-dogfood.sh
scripts/status-dogfood.sh
scripts/stop-dogfood.sh
```

dogfood Frontend／Backend／ready gate／LiveKitはそれぞれ15173／18000／14174／17880を使うため、
5173／8000／4174／7880を使うdev・integration Profileと同時起動できる。dogfoodのOllama、
VOICEVOX、LiveKitはapplication orchestratorから見て`external`であり、environment runの所有対象にも`stop`の対象にもならない。Ubuntu-dogfoodのsystemd targetがこれらのservice lifecycleを別途所有する。
Chroma／RAGはWave 2受入まで無効で、起動・probe・所有を行わない。

## Ubuntu-devの初期セットアップ

```bash
sudo apt update
sudo apt install -y git curl build-essential python3 python3-venv

# Node.js（LTS）
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs

# Backend 仮想環境
scripts/setup-backend.sh
```

Ollama、Docker、VOICEVOXの導入と起動はUbuntu-dogfood側で行う。別distributionの作成、systemd有効化、専用service user、独立clone、data／state／log directoryの構築手順は`infra/dogfood/README.md`を参照する。

疎通確認:

```bash
curl http://localhost:11434/api/tags
curl http://127.0.0.1:50021/version
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
| `dev` | 通常のローカル開発 | Frontend、Backend、external Ollama／VOICEVOX／LiveKit、Whisper |
| `test-mocked` | ブラウザ内 mock を使う独立 E2E | Frontend、browser mock Backend |
| `integration-text` | 実テキストチャット | Frontend、Backend、external Ollama |
| `integration-voice` | 実音声チャット | Frontend、Backend、external Ollama／VOICEVOX／LiveKit、Whisper |
| `dogfood` | 継続利用する運用相当環境 | Frontend、Backend、external Ollama／VOICEVOX／LiveKit、Whisper |

起動スクリプトはサービス起動前に中央の解決処理でProfileを検証する。実行時レポートと解決済みProfileは解決済みデータルートの`runtime/`配下にのみ保存する。Playwrightは各スイート専用のテスト用データルートを設定し、`runtime/standalone/`へ環境レポートを、`frontend/test-results/<suite>/`へテスト証跡を保存する。レポートには環境IDと正規化済みパスを記録し、秘密値や会話本文は記録しない。

`derivedEnvironment`の接続先に加え、`OLLAMA_CHAT_MODEL`、`OLLAMA_CLASSIFIER_MODEL`、`OLLAMA_EXTRACTOR_MODEL`、`WHISPER_MODEL`、`OLLAMA_CONTEXT_TOKENS`、応答予約量、履歴・入力・モデルコンテキスト上限は中央の解決結果から起動対象へ渡される。`scripts/start-backend.sh`は`backend/.env`をProfile解決前に読み込む。`DS_PROFILE_REPORT`で既存の解決済みレポートを指定しない場合は`.env`のモデル設定を解決結果へ取り込み、指定した場合はレポートの解決済み設定を優先する。不正な文字列、正でない整数、応答予約量が実行時コンテキスト以上、または実行時コンテキストがモデル最大コンテキストを超える指定は、サービス起動前に拒否される。

依存の `source` が `managed` の場合は対応するローカルプロセスまたはコンテナを起動して readiness を待つ。`external` の場合は起動せず、Profile の `readinessUrl` で外部サービスの準備完了だけを確認する。`disabled` の依存は起動しない。

`dev` では次の順序で起動確認を行う。

1. `scripts/setup-backend.sh` で Backend の仮想環境と依存関係を準備する
2. Ubuntu-dogfood所有のOllamaについて`http://localhost:11434/api/tags`のreadinessを確認する
3. Ubuntu-dogfood所有のVOICEVOXについて`http://127.0.0.1:50021/version`のreadinessを確認する
4. managed adapterが`start-backend.sh --host localhost --port 8000 --reload`で FastAPI Backend を起動し、`http://localhost:8000` を確認する
5. Frontend 開発サーバーを起動する

OllamaまたはVOICEVOXが未起動の場合、`dev`または`integration-*`は共通serviceを作成・起動せずreadiness失敗として終了する。構築と復旧は`infra/dogfood/README.md`に従う。

`VOICE_CHAT_E2E_BACKEND`、`CHAT_E2E_BACKEND`、`CHAT_E2E_BACKEND_ORIGIN`、`VOICE_CHAT_E2E_BACKEND_REPORT` は中央 resolver だけが解釈する非推奨の互換入口である。新しい起動・テスト設定では `DS_PROFILE` と `DS_PROFILE_REPORT` を使用する。`DS_PROFILE` と旧指定が異なる構成を示す場合や、複数の旧指定を単一 Profile に変換できない場合は、サービス起動前にエラーとなる。

## 個別起動スクリプト

| スクリプト | 役割 |
|---|---|
| `scripts/setup-backend.sh` | Backend の `.venv` を作成し、`backend/requirements.txt` をインストールする |
| `scripts/start-backend.sh` | resolved Profile由来の明示的な`--host`、`--port`、任意の`--reload`を受け、FastAPIを起動する |
| `scripts/start-frontend.sh` | Frontend 開発サーバーを起動する |
| `scripts/start-voice-chat-e2e.sh` | 音声チャット E2E 用。`DS_PROFILE` 未指定時は `integration-voice` を選択し、`test-mocked` では Frontend のみを起動する |
| `scripts/start-dogfood.sh` | dogfood Profileとidentityを固定して起動する |
| `scripts/status-dogfood.sh` | dogfoodのowned managedとunowned externalを区別して表示する |
| `scripts/stop-dogfood.sh` | dogfoodの所有reportに記録されたmanagedプロセスだけを停止する |

`scripts/start-backend.sh` は仮想環境の作成や依存インストールを自動実行しない。初回または依存関係の更新時は `scripts/setup-backend.sh` を別に実行する。セットアップ失敗は `Backend setup failed` と失敗工程、起動環境の不足は `start-backend.sh` の対象ファイル名を含むエラーで判別できる。Backend プロセスの起動後は、その終了ステータスが呼び出し元へ伝播する。

Backend単体起動とdev／integrationの`scripts/start-all.sh`はOllamaやVOICEVOXを準備・起動・停止しない。共通推論serviceの操作はUbuntu-dogfoodの`scripts/dogfood/`入口だけを使う。開発用の`scripts/start-all.sh`と実Backendを使うE2E用の`scripts/start-voice-chat-e2e.sh`は、どちらも準備段階で`scripts/setup-backend.sh`を実行し、起動段階で共通の`scripts/start-backend.sh`を使う。

## 音声チャットの依存関係

音声チャットでは Backend の `AudioPipelineService` が STT、LLM、TTS を順に実行する。

- TTS は `VOICEVOX_BASE_URL` を参照し、未設定または空文字時は `http://127.0.0.1:50021` に接続する
- `VoicevoxClient` は `/audio_query` と `/synthesis` を呼び出す
- 共通環境オーケストレーターの VOICEVOX adapter は Profile の `readinessUrl` で `/version` を確認する
- Whisper は外部サービスではない。旧WebSocket baselineはBackend main process内、LiveKit Conversation CoreはBackendが所有する専用child process内で`WHISPER_MODEL`（既定`medium`）を初回利用時にロードする
- LiveKit Conversation CoreのWhisper隔離境界は`WHISPER_LOCK_TIMEOUT_SECONDS`（既定5秒）と`WHISPER_INFERENCE_TIMEOUT_SECONDS`（既定45秒）で調整する。timeout後はworker processを破棄し、次requestでmodelを再生成する。
- 共通環境オーケストレーターは prepare で Whisper モデルを`<data root>/cache/huggingface/hub`へ準備し、Backend 実行時も同じ保存先を使う
- `<data root>/cache/huggingface/` は Git 管理対象外である。Backend を単体起動する場合は初回利用時に取得が発生し得るため、オフライン環境では事前にこのキャッシュを用意する
- `WHISPER_MODEL` を変更した場合、prepare時のcache名・ダウンロード対象・Backend実行モデルが一緒に切り替わる

## ChromaDB

ChromaDB は外部プロセスではなく、Backend プロセス内で `chromadb.PersistentClient` として利用する。永続化先は解決済みdata rootの`chroma/`である。

リポジトリ配下に永続データが作られるため、開発環境では作業ユーザーが `backend/app/data` を作成・書き込みできる権限を持っている必要がある。

dogfood ChromaはWave 2受入後にSQLite正本から構築する。

## LiveKit開発・検証環境

devは`infra/livekit/compose.yaml`を起動し、Backendへ`LIVEKIT_URL`、`LIVEKIT_API_KEY`、`LIVEKIT_API_SECRET`を渡す。`/voice/livekit`はIssue #113で追加した基盤検証用の一時入口であり、Wave 3の後続実装では通常の会話UIへLiveKit sessionを直接統合する。既存WebSocket音声UIをWave 3の実装先として拡張したり、完成後に別工程でLiveKitへ切り替えたりしない。LiveKit Serverの起動とkey/secret生成は開発者が行う。

## テストとの関係

テスト層と外部サービス実接続の扱いは `docs/testing-policy.md` を参照する。VOICEVOX / Whisper / ChromaDB / Ollama の実接続を完了条件として報告する場合は、同ドキュメントのインテグレーションテスト方針に従い、実サービスへの接続ログを一次証跡にする。
