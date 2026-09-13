# backend

`digital-souls`の自作バックエンド（FastAPI）。現在の全体構成は[アーキテクチャ](../docs/system-architecture.md)、概念と実装名の対応は[用語集](../docs/glossary.md)を参照する。

## 主な責務

| 領域 | 実装入口 |
|---|---|
| Character Card V3・Book・カタログ・立ち絵 | `app/characters/`、`app/routers/character_catalog.py` |
| テキスト・Speech/Text共通会話処理 | `app/chat_service.py`、`app/_chat_runtime.py`、`app/conversation_core/` |
| LiveKit音声・control・接続管理 | `app/livekit_transport/`、`app/voice_session/`、`app/routers/livekit.py` |
| 会話履歴・スレッド・UI設定 | `app/conversation_history/`、`app/ui_settings/` |
| 長期記憶の形成・検索・統合・管理 | `app/memory/`、`app/routers/memory_management.py` |
| privacy・prompt・用途別推論 | `app/privacy/`、`app/prompting/`、`app/inference/` |
| 必要なturnだけの画面参照 | `app/screen_perception/`、`app/routers/screen_perception.py` |
| 外部MCP・会話利用・承認・管理 | `app/external_mcp/`、`app/tool_use/`、`app/addon_action/`、`app/addon_admin/` |
| 会話外活動・Life State | `app/character_life/`。DBOS基盤は既定無効、dev/test対象 |

`POST /chat`のHTTP会話に加え、実行中Conversation Sessionでは音声とテキストを同じCoreへ渡す。正式な音声経路はLiveKitであり、`app/routers/ws.py`の旧WebSocketはbaseline／互換用である。

## runtimeデータ

`DS_DATA_DIR`をSQLite、Chroma、runtime report、cacheの単一data rootとして使用する。
`DS_ENVIRONMENT_ID`は`dev`、`test`、`dogfood`のいずれかで、未指定時は`dev`、未指定のdata rootは`backend/app/data`である。
dogfoodはリポジトリ外の絶対パスを指定する。起動時に`.environment-identity.json`を検証し、不一致ならデータストアを開く前に終了する。

会話履歴は`conversation-history.db`、承認済み長期記憶等は`persona-memory.db`、検索indexは`chroma/`へ分ける。
パスの正本は[`app/runtime_paths.py`](app/runtime_paths.py)。Character Lifeのdomain DB・DBOS管理DB等、機能固有の保存先とbackup条件は各運用手順を参照する。Chromaを記憶の正本や唯一のbackup対象にしない。

## キャラクター・スレッド・UI設定

主なAPIは次のとおり。全体の登録は[`app/main.py`](app/main.py)と各routerを参照する。

- `GET /`、`GET /health/ready`、`GET /health/inference`：稼働・readiness・推論状態。
- `GET /characters`、`POST /characters/rescan`、`GET /characters/{character_id}/assets/standing/default.png`：カタログと立ち絵。
- `POST /characters/{character_id}/conversations`、`GET /characters/{character_id}/conversations`、`GET /characters/{character_id}/conversations/archived`：スレッド作成・通常一覧・アーカイブ一覧。履歴取得・名称変更・アーカイブ・復元・物理削除もスレッド単位で行う。
- `GET /ui-settings`と配下の更新操作：立ち絵配置、履歴範囲、キャラクター表示、キャラクター／スレッドのピン留め。

スレッド名は最初の履歴保存可能なユーザー発言から一度だけ決定論的に生成し、手動名を上書きしない。
UI設定はローカル単一ユーザー`local`へ紐付ける。SNSログイン等の実ユーザーIDとの統合は後続範囲である。

キャラクターカタログはリクエスト時に`characters/`を再走査し、有効なCharacter Cardだけを返す。
立ち絵URLはBackendが生成し、character境界、variant、PNG、symlink脱出を検証する。
立ち絵は`ETag`と`Cache-Control: no-cache`を返し、`If-None-Match`による再検証に対応する。Frontendはリポジトリ内のファイルパスを直接組み立てない。

アーカイブは短期会話履歴を保持したまま通常利用から外す操作である。
スレッドの物理削除では対象conversationと全turnだけをSQLiteから削除し、短期履歴は復元できなくなる。
`secure_delete`を有効にし、物理削除後のWAL後処理に失敗した場合は本文を含まない再試行情報を保存して起動時に再試行する。
どちらもRAG長期記憶は暗黙変更しない。既存backup・snapshot・ファイルシステム上の複製からの消去までは保証しない。

## 長期記憶

保存済み会話履歴から非同期に候補を形成し、privacyとpositive allowlistを通過した記憶だけをSQLiteへ保存する。
既存の非同期形成経路の許可型は`EPISODIC_EVENT / USER_PREFERENCE / INTERACTION_PREFERENCE`である。
SQLiteの変更とindex outboxを同じtransactionに記録し、Chromaへ同期する。検索結果はSQLite正本で所有character・状態・期限・policy等を再検証する。

**長期記憶・暫定記録の閲覧、訂正、物理削除は実装済み**である。
長期記憶の物理削除はSQLite commit後にChroma削除を同期試行し、失敗時はoutboxの再試行で回復する。スレッド削除とは別操作である。
既存記憶のconsolidationも、複数Episodeからの意味抽象化・Reflection形成・人格適応とは区別する。
設計と受入は[Wave 2 ADR](../docs/decisions/wave2-memory-formation-retrieval-2026-08.md)、[受入記録](../docs/wave2-acceptance-2026-08.md)、現行型は[`admission/contracts.py`](app/memory/admission/contracts.py)を参照する。

Episode / Factは`app/memory/episodic/`の独立レコード・版・出典・参照で保存する。
`EpisodicRegistrationService`は同threadのFact照合・補足訂正・冪等登録を扱う。
`/characters/{character_id}/episodic-memories`のGETで監査し、PATCH / DELETEでは版と冪等keyを指定してFactを訂正・削除する。
新モデルへの会話自動抽出workerの起動接続は後続作業である。
[保存・登録・検索の境界](../docs/system-architecture.md#episode--factの保存登録管理基盤)を参照する。

## セットアップ・起動

通常はリポジトリルートの`scripts/start-all.sh`を使うProfile＋Docker構成で起動する。
共有推論サービス、dev用LiveKit、data root、停止手順まで含む[開発環境](../docs/development-environment.md)に従う。

ホスト上の検査や互換・単体開発用のPython環境は、初回または依存更新時に次で作成する。

```bash
scripts/setup-backend.sh
```

このスクリプトは`backend/.venv`の作成と実行時依存関係のインストールだけを行い、Backendは起動しない。
テスト用依存・実行条件は[テスト方針](../docs/testing-policy.md)を参照する。
画面の画像decodeにはPillowを使用するが、Vision Target未設定で画像送信やVision推論は開始しない。

Docker経路と区別して、互換・単体開発のためにBackendだけをforeground起動する場合は次を使う。

```bash
scripts/start-backend.sh --host localhost --port 8000 --reload
```

解決済みProfileと一致するhost・port・reload設定を指定する。構築済みの`.venv`が必要で、セットアップは自動実行しない。
Backendプロセスが終了した場合は終了ステータスが呼び出し元へ伝播する。

## 推論・STT・任意機能

LLMとprompt予算は[`backend/.env.example`](.env.example)の`INFERENCE_TARGET_*`等で設定する。
Provider／Model、入力・出力token上限、timeout、同時実行数を用途別に解決する。
Chatの入力＋出力上限は`LLM_CONTEXT_TOKEN_LIMIT`以下とし、不正値や旧Ollama用途別変数は起動時エラーにする。
Provider AdapterはOllama／OpenAI API／Codex runtimeに対応し、Coreから接続先を直接選ばない。

STTは`WHISPER_BASE_URL`で指定した共有Whisper HTTP serviceを使用する。
現行Backendで`WHISPER_MODEL`に基づいてGPUモデルを初期化するわけではない。モデル設定と共有serviceの起動・変更は[開発環境](../docs/development-environment.md)と[dogfood運用](../infra/dogfood/README.md)を確認する。

画面知覚を使う環境だけ任意の`INFERENCE_TARGET_VISION`を設定する。
共有ONは画像送信の包括許可ではなく、Coreが現在発言と許可された履歴から参照要否を3分岐する。
参照判断LLMはChat Targetを`screen-reference` callerとして利用し、Provider・対象・同意を選べない。
生画像とVision観測はrequest終了時に破棄し、privacy処理済み回答だけを画面由来provenance付きで短期履歴へ保存する。
この由来を持つturnと派生turnはMemory Formationへ投入しない。

設定と検証の詳細は[Inference運用](../docs/inference-operations.md)、[会話からのMCP利用](../docs/tool-use.md)、[Addon管理](../docs/addon-admin.md)を参照する。
Character Lifeは[専用手順](../docs/character-life-operations.md)で明示的に有効化する。DBOS基盤やLife Stateの存在を、SELF Episode・Reflection・人格・Skillの全体実装完了と解釈しない。
