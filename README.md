# digital-souls

キャラクターとの継続的なテキスト・音声会話を中心に、記憶、外部ツール、会話外の活動を組み合わせるパートナー型AIのプロジェクトです。自作Backend（FastAPI）と自作Frontend（Vite + Svelte + TypeScript）で構成します。

初期キャラクターは[光織（Miori）](characters/miori/)です。runtimeの定義はCharacter Cardを正本とし、記録係などの固定業務ではなく性格を中心に設計しています。役割は実際の会話や活動の文脈で扱います。

LiveKitのVAD・発話区間・入力世代をBackendへ移す#358の導入はPR #408でmainへ反映済み。
FE／BEはprotocol 2.0へ一組で更新する。残る品質・実接続・実マイク受入等は#424で追跡する。
採用済みIrodoriの品質は#423、既存の再接続・初回推論待ち・相槌は#350で改善する。
[共通実行指示書](docs/voice-quality-350-423-424-requirements.md)、[移行契約](docs/voice-backend-migration-contract.md)、
[検証記録](docs/validation/voice-backend-vad-358.md)を参照する。この作業ブランチでは開始準備・取消・失敗表示とSTT／対応LLMの事前準備を実装している。正式な性能・品質受入は未完了。

## 現在の実装範囲

この一覧はリポジトリ内の実装を説明するもので、すべての機能が既定で有効、またはdogfoodで受入済みであることを意味しません。進捗・残課題はGitHub Issues、検証条件は各受入記録を参照してください。

| 領域 | 実装と境界 |
|---|---|
| キャラクター・会話UI | Character Card V3、Character Book、立ち絵、キャラクター別スレッド、名称変更、ピン留め、アーカイブ・復元・削除、PC／compactレイアウト |
| 会話履歴 | `character_id`と`conversation_id`で分離してSQLiteへ保存。同じスレッドの保存済み履歴を再開し、別スレッドの生会話をそのまま混ぜない |
| 音声・テキスト併用 | LiveKitの継続音声入力、STT、応答の逐次生成・TTS・再生、割り込み、再接続。同じ実行中Sessionへのテキスト入力と受理結果照合にも対応。旧WebSocket音声はbaseline／互換用 |
| 長期記憶 | privacy判定・positive allowlistを経た非同期形成、検索、閲覧・訂正・物理削除、既存記憶の統合。SQLiteが正本、Chromaは派生検索index |
| 推論 | 用途別TargetをOllama／OpenAI API／Codex runtimeへ割り当てる共通境界。Provider・Model・上限は環境設定で解決し、暗黙のfallbackは行わない |
| 画面知覚 | 利用者が選んだ単一のモニター／ウィンドウ／ブラウザタブを必要なturnだけ参照。Vision Targetは任意設定で、共有ONだけでは画像を送信しない |
| 外部ツール・管理 | 登録済みMCPへの接続、会話からのTool／Resource利用、接続管理、操作承認・確認・結果回復。実行前の権限・引数・送信内容の検証を共通化 |
| Character Life | DBOSによる会話外活動、Life State、許可・実行履歴の基盤。**既定無効・dev/test対象**。SELF Episode、Reflection、人格適応、Skill等との全体接続は未完了 |

既存の非同期形成経路の許可型は`EPISODIC_EVENT`、`USER_PREFERENCE`、`INTERACTION_PREFERENCE`です。複数Episodeからの意味抽象化、独立した内省記憶、経験に基づく人格適応は、既存の記憶統合と区別します。[用語集](docs/glossary.md)に、設計上の概念と現在の実装名の対応をまとめています。

LiveKitへの移行・音声／テキスト併用の実装があることと、遅延・回復・連続運用の課題が解消したことは別です。[混在Session受入](docs/conversation-session-acceptance.md)、[dev回復記録](docs/conversation-session-dev-recovery.md)、[連続操作試験](docs/conversation-session-dev-operations.md)に既知の制約と証跡を残しています。無期限に同じ実行Sessionを維持できる保証はありません。

Episode / Factの独立した保存・同thread登録・版付き参照・検索投影・管理UIも実装しています。
会話全体からEpisode / Factを自動抽出するworkerを、永続予約と非同期実行で接続しています。
採用抽出器はv13-compact18です。固定評価の残る未達とdev総合受入は[検討記録](docs/episodic-quality-iterations-2026-09.md)で区別します。
詳細は[アーキテクチャ](docs/system-architecture.md#episode--factの保存登録管理基盤)を参照してください。

## 構成とデータの扱い

```text
ブラウザ（テキスト・マイク・立ち絵・管理UI）
  ├─ HTTP ───────────────────┐
  └─ LiveKit（音声・control） ─┤
                              ▼
                  FastAPI / Conversation Core
                    ├─ Character Card / PromptBuilder
                    ├─ 会話履歴・長期記憶（SQLite → Chroma）
                    ├─ 共通Inference Router
                    ├─ MCP / Execution Gate
                    └─ Character Life（任意有効化）
                  STT: 共有Whisper HTTP service
                  TTS: VOICEVOX
```

会話履歴と長期記憶は別の保存・削除境界です。スレッドを削除しても長期記憶を暗黙削除しません。画像や外部ツールのnative payloadをそのまま長期記憶へ登録せず、履歴・長期記憶・外部送信それぞれのprivacy境界を通します。秘密値・会話原文をログやIssueへ転記しないでください。

Live2D／VRM、Desktop／Discord等の追加クライアント、複数キャラクターのグループ会話、Mac miniへの常時稼働環境移行は、現行ブラウザ実装とは分けた拡張方針です。詳細は[アーキテクチャ](docs/system-architecture.md)と[ロードマップ](docs/roadmap.md)を参照してください。

## 開発環境で起動する

Linux／WSL2での作業を前提とします。通常のBackend／Frontend起動はEnvironment ProfileとDockerを使用します。初回は[開発環境](docs/development-environment.md)の手順でDocker・共有推論サービス・dev用LiveKit・設定を準備してください。以下は**それらの準備後**の起動例です。

```bash
export DS_PROFILE=dev
export DS_ENVIRONMENT_ID=dev
export DS_DATA_DIR="${HOME}/.local/state/digital-souls/dev"
export DS_ENVIRONMENT_RUN_REPORT="${DS_DATA_DIR}/runtime/dev/environment-run.json"
export DS_PROFILE_REPORT="${DS_DATA_DIR}/runtime/dev/resolved-profile.json"
scripts/start-all.sh

# 同じ環境変数・reportを使って状態確認、停止
environments/status.sh
environments/down.sh
```

既定のdev UIは`http://localhost:5173`、Backendは`http://localhost:8000`です。起動済みのOllama・VOICEVOX・Whisper・LiveKitはProfileから見て外部依存です。アプリの起動・停止で共有推論サービスを作成・停止しません。別途起動したdev用LiveKitの終了手順も開発環境文書を参照してください。

`DS_PROFILE`はサービス構成、`DS_ENVIRONMENT_ID`はデータの環境識別、`DS_DATA_DIR`は単一のruntime data rootです。dev/testとdogfoodでデータや所有reportを共有しないでください。dogfoodの配備・backup・restoreは専用手順を使い、mainへのマージだけではdeployしません。

個別起動・依存設定は[Backend](backend/README.md)・[Frontend](frontend/README.md)、検査の準備と層分けは[テスト方針](docs/testing-policy.md)、実行コマンドは[package.json](package.json)を参照してください。モックE2Eの成功は実サービス接続や実マイクの受入を代替しません。

## ドキュメント

| 読みたい内容 | 参照先 |
|---|---|
| 用語・実装名・設計との違い | [用語集](docs/glossary.md) |
| 現在の責務境界 | [システムアーキテクチャ](docs/system-architecture.md) |
| 採用判断・優先関係 | [ADR一覧・運用](docs/decisions/README.md) |
| 開発目標と分解 | [ロードマップ](docs/roadmap.md) / [拡張計画](docs/enhancement-plan.md) |
| 起動・環境・配備 | [開発環境](docs/development-environment.md) / [インフラ方針](docs/infrastructure-policy.md) / [dogfood運用](infra/dogfood/README.md) |
| 音声品質改善の合意・段階評価・PR分割 | [#350・#423・#424共通実行指示書](docs/voice-quality-350-423-424-requirements.md) |
| 推論の設定・検証 | [Inference運用](docs/inference-operations.md) |
| 外部ツール | [MCP基盤](docs/external-mcp-foundation.md) / [会話からの利用](docs/tool-use.md) / [Addon管理](docs/addon-admin.md) |
| 会話外の活動 | [Character Life運用](docs/character-life-operations.md) |
| 品質・開発規約 | [テスト方針](docs/testing-policy.md) / [リポジトリ運用](docs/repository-policy.md) / [AGENTS.md](AGENTS.md) |

## ディレクトリ構成

```text
digital-souls/
├─ backend/          # FastAPI・会話・記憶・推論・ツール・活動基盤
├─ frontend/         # ブラウザUI・LiveKit client
├─ characters/       # Character Cardと編集用補助資料
├─ contracts/        # FE/BE間の共有契約
├─ environments/     # Profile・環境の解決と起動管理
├─ infra/            # Compose・dogfood構築と運用
├─ whisper_service/  # 共有STT service
├─ scripts/          # 起動・検証・運用の入口
└─ docs/             # 用語・現行構成・ADR・運用・受入証跡
```

設計判断はADR、具体的な作業・依存関係・完了条件はIssuesに残します。実装フローにはTAKTを使用し、ブランチ運用と文書更新の責務は[リポジトリ運用方針](docs/repository-policy.md)に従います。
