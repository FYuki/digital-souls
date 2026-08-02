# 開発ロードマップ

## 目的

`digital-souls` の開発を、人格設計・基盤実装・音声対応・長期記憶・配信連携の順に段階的に進める。

> **2026-06-17 方針転換**: AIRIフォーク利用を取りやめ、自作BE（FastAPI）+ 自作FE（Vite + Svelte）構成に移行した。
> 理由・経緯は `docs/decisions/` を参照。

> **2026-07-09 方針転換**: MVP（テキスト+音声チャット、RAG基盤）完了を受け、旧Phase 5〜8のタスク列挙を白紙化し、
> post-MVPをWave 1〜4構成に再編した。経緯は `docs/decisions/post-mvp-enhancement-2026-07.md`、
> 詳細な設計・タスク分解は `docs/enhancement-plan.md` を参照。

## Phase 0〜4: MVP（完了）

以下は完了済みの履歴として簡潔に残す。詳細な経緯は各 `docs/decisions/` を参照。

### Phase 0: 方針整理

[x] リポジトリ構成を決める
[x] GitHub運用方針を決める
[x] AIRIをコア候補として調査する（→ 自作方針に転換、フォークなし）
[x] Live2D / VRM / 静止画UIの役割を整理する
[x] Mac mini / Windows / Cloud VMの役割を整理する

### Phase 1: 人格設計

[x] 光織の人格設定を作成する
[x] 光織の世界観を作成する
[x] 記憶方針を定義する
[x] 複数人格対応を前提に `characters/` 構成を整える

### Phase 2: 開発環境整備

[x] Windows + WSL2で開発環境を構築する
[x] Docker利用方針を決める
[x] Ollamaで軽量LLMを検証する
[x] Gemma 4B級モデルの応答品質と速度を検証する
[x] 将来のMac mini移行手順を整理する

### Phase 3: テキストチャット基盤（自作BE/FE）

[x] リポジトリ構成を自作BE/FE用に整備する（backend/, frontend/）
[x] FastAPI プロジェクト基盤を構築する（Ollama接続・キャラクターロード）
[x] Vite + Svelte テキストチャットUIを実装する
[x] キャラクター指定方式（リクエストパラメータ・ステートレス）を実装する

### Phase 4: 音声対応

[x] BEにWebSocketエンドポイントを追加する
[x] STT（Whisper）+ TTS（VOICEVOX）パイプラインを実装する
[x] FEにマイク入力（VAD対応）・音声再生UIを実装する（テキスト/音声をWS接続に統合、#14）
[x] BEのWS音声フレームに処理中キュー（1件保持・上書き）を実装する

Phase 4の未完了項目だった「WebSocketの遅延を計測し、LiveKit移行の必要性を判断する」は、
新プロトコル設計（会話状態管理）を前提に判断する必要があるため **Wave 3** へ移動した。

RAG（Chroma + nomic-embed-text）のMVP構成は実装済みだが、`RAG_ENABLED=false` がデフォルトのため
現状は無効化されている。本稼働化は **Wave 2** で扱う。

## Post-MVP: Wave 1〜4

MVP完了時点で判明したギャップ（多ターン会話、RAG本稼働、
`character_id`スキーマ統一、LLM/TTSの逐次処理による遅延 等）を踏まえ、
「続く → 覚えている → 自然に話せる → 役に立つ」の順で再編する。
各Waveの詳細タスク・完了イメージ・依存関係は `docs/enhancement-plan.md` を参照。

### Wave 1: 会話が「続く」（短期記憶・基盤整備）

- [x] SQLite会話履歴schema（開発用テストDBは削除して現行schemaを空状態から再作成。正式なschema v2はconversation／turnを保持して、`conversations.archived_at`とWAL後処理の再試行metadataを含むv3へmigrationし、契約外schemaは起動時に拒否）
- [ ] 共通privacy scannerと履歴sanitizer（直接識別値を保存前にマスクし、マスク不能時は本文を保存しない）
- [x] 会話履歴のプロンプト注入（同じ`character_id` / `conversation_id`の直近N往復だけを復元してLLMへ渡す。RAG無効時も履歴を記録する）
- [x] PromptBuilderによる合成の一元化（Character Card V3 / RAG記憶 / マスク済み会話履歴 / 現在発言 / 最終指示の順序とtoken budgetを固定）
- [ ] 設定のenv化（`OLLAMA_CHAT_MODEL` 等、Whisperモデルサイズ、履歴注入数N）
- [x] Backend／Frontendのconversation lifecycle統合
- [x] スレッド一覧・再開・アーカイブ・物理削除インターフェース（物理削除はSQLite上のconversationと全turnだけが対象。RAG長期記憶は変更せず、既存backup・snapshot・ファイル複製の消去は保証しない）

### Wave 2: 「覚えている」（RAG本稼働）

- [ ] 文脈依存の機微情報assessment基盤（health、心理状態、金融状況、第三者情報等を交換可能なclassifierと固定corpusで判定し、保存可否とは分離）
- [ ] RAG admission policy（共通scannerと`PrivacyAssessment`を再利用し、positive allowlist型だけを許可）
- [ ] 承認済み記憶schema（SQLite `approved_memories`を正本とし、全レコードへ`character_id`と`policy_version`を付与）
- [ ] transactional outbox（SQLiteの`approved_memories` hard delete後にChromaを同期削除し、失敗時はmetadata-only outboxと定期reconciliationでSQLite正本へ収束させる）
- [ ] Chroma派生index化（承認済み記憶だけを登録し、取得時にSQLiteの状態・TTL・policy versionと絶対禁止findingを再検証）
- [ ] RAG検索品質検証 → `RAG_ENABLED=true` デフォルト化
- [ ] positive allowlist型の記憶候補抽出（絶対禁止・機微情報判定を通過した構造化候補だけを扱う）
- [ ] 自動記憶昇格（会話サマリ→長期記憶候補化、光織が確認し、未確認候補はRAGへ保存しない）
- [ ] 時系列照合（日付メタデータ+時期検索）
- [ ] 記憶の閲覧・訂正・物理削除インターフェース

### Wave 3: 「自然に話せる」（会話状態管理による双方向会話）

- [ ] 会話状態マシン（idle/listening/thinking/speakingをBEが管理）
- [ ] WSプロトコル拡張（state / text_delta / audio_chunk / audio_end / cancel）
- [ ] LLMストリーミング（Ollama stream:true）
- [ ] 文単位ストリーミングTTS
- [ ] barge-in（割り込み対応、エコー対策含む）
- [ ] 遅延計測の指標化 → LiveKit移行判断（Phase 4からの移動タスク）
- [ ] 既存の音声1件保持キューの再設計

### Wave 4: 「役に立つ」（後続・優先度低）

- [ ] ツール実行基盤+農業日誌
- [ ] ClaudeClient実装・プロバイダ切替
- [ ] 2人目キャラクター検証
- [ ] Discord Bot / Mac mini常時稼働 / Live2D
