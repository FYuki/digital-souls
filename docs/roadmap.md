# 開発ロードマップ

## 目的

`digital-souls` の開発を、人格設計・基盤実装・音声対応・長期記憶・配信連携の順に段階的に進める。

この文書はPhase／Waveごとに実現する機能を示し、タスクの進捗、依存関係、完了条件は
GitHub Issuesで管理する。ロードマップではチェックボックスや個別Issueの状態を管理しない。

> **2026-06-17 方針転換**: AIRIフォーク利用を取りやめ、自作BE（FastAPI）+ 自作FE（Vite + Svelte）構成に移行した。
> 理由・経緯は `docs/decisions/` を参照。

> **2026-07-09 方針転換**: MVP（テキスト+音声チャット、RAG基盤）完了を受け、旧Phase 5〜8のタスク列挙を白紙化し、
> post-MVPをWave 1〜4構成に再編した。経緯は `docs/decisions/post-mvp-enhancement-2026-07.md`、
> 詳細な設計・タスク分解は `docs/enhancement-plan.md` を参照。

## Phase 0〜4: MVP（完了）

以下は完了済みの履歴として簡潔に残す。詳細な経緯は各 `docs/decisions/` を参照。

### Phase 0: 方針整理

- リポジトリ構成とGitHub運用方針
- 自作Backend／Frontendを中核とするアーキテクチャ
- Live2D、VRM、静止画UIの役割分担
- Mac mini、Windows、Cloud VMの役割分担

### Phase 1: 人格設計

- 光織の人格、世界観、記憶方針
- 複数人格に対応できる `characters/` 構成

### Phase 2: 開発環境整備

- Windows + WSL2の開発環境
- Docker利用方針
- ローカル軽量LLMの開発・検証環境
- Mac miniへの移行方針

### Phase 3: テキストチャット基盤（自作BE/FE）

- 自作Backend／Frontendのチャット基盤
- テキストチャットUI
- キャラクターを指定した会話

### Phase 4: 音声対応

- ブラウザ音声会話
- ローカルSTT／TTSによる音声処理基盤
- テキストチャットと音声チャットの統合

Phase 4の未完了項目だった音声遅延の計測と通信方式の再評価は、**Wave 3** へ移動した。

RAG基盤はMVPで構築済みとし、本稼働化は **Wave 2** で扱う。

## Post-MVP: Wave 1〜4

MVP完了時点で判明したギャップ（多ターン会話、RAG本稼働、応答遅延等）を踏まえ、
「続く → 覚えている → 自然に話せる → 役に立つ」の順で再編する。
各Waveの設計詳細は `docs/enhancement-plan.md` と `docs/decisions/`、タスク管理はGitHub Issuesを参照する。

### Wave 1: 会話が「続く」（短期記憶・基盤整備）

- 会話履歴とスレッドの永続化
- 会話履歴のprivacy保護
- 保存済み履歴を利用する複数ターン会話
- 実行時設定の外部化
- Backend／Frontendで統一された会話ライフサイクル
- スレッドの一覧、再開、アーカイブ、復元、削除

### Wave 2: 「覚えている」（RAG本稼働）

設計上の正本: `docs/decisions/wave2-memory-formation-retrieval-2026-08.md`

- 文脈依存の機微情報判定とpositive allowlistによる保存判定
- SQLiteを正本、Chromaを派生indexとする長期記憶・検索基盤
- 会話履歴からの非同期な長期記憶形成
- 機微なqueryで検索を抑止するprivacy境界
- RAG検索品質の評価と標準有効化
- 記憶と記録の時系列照合
- 人格記憶・暫定記録の閲覧、訂正、物理削除
- idle時のpersona memory consolidation
- 開発とdogfoodのruntime data、service、backup、deployの分離

### Wave 3: 「自然に話せる」（LiveKitによる双方向音声会話）

設計判断: `docs/decisions/post-mvp-enhancement-2026-07.md`

- LiveKit Room、Participant、Track、接続認証によるrealtime media transport
- transport非依存の音声session、utterance、response、playback lifecycle
- 継続microphone入力とVAD eventによる発話区間管理
- LLM応答テキストの逐次配信
- VOICEVOX音声の逐次合成とCharacter AudioTrack再生
- response単位の世代管理、cancel、遅延出力の破棄
- Character発話中のbarge-inと最新発話の優先
- 中断応答と完了・失敗を区別する履歴、privacy、記憶整合性
- 音声session状態UI、再接続、障害回復
- 会話品質、遅延、割り込み、再接続の計測と自動・dogfood受入

### Wave 4: 「役に立つ」（後続・優先度低）

- パーソナルAI向けツール連携
- LLMプロバイダの拡張
- クライアント、常時稼働、アバター連携の拡張

### Epic C: 複数キャラクター会話

- User + 光織 + 葵のテキストグループチャット
- 共有会話とCharacter別episodic memoryの分離
- LiveKit Roomへの複数Character音声統合
