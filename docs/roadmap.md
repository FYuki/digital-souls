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

Phase 4の未完了項目だった音声遅延のbaseline計測とLiveKitへの移行は、**Wave 3** へ移動した。

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

基盤の設計判断: [Wave 2記憶形成・検索方針](decisions/wave2-memory-formation-retrieval-2026-08.md)。
後続の記憶責務再編は次節と[Episode・Fact・Semantic境界ADR](decisions/episode-fact-semantic-boundaries-2026-09.md)を参照する。

- 文脈依存の機微情報判定とpositive allowlistによる保存判定
- SQLiteを正本、Chromaを派生indexとする長期記憶・検索基盤
- 会話履歴からの非同期な長期記憶形成
- 機微なqueryで検索を抑止するprivacy境界
- RAG検索品質の評価と標準有効化
- 記憶と記録の時系列照合
- 人格記憶・暫定記録の閲覧、訂正、物理削除
- idle時のpersona memory consolidation
- 開発とdogfoodのruntime data、service、backup、deployの分離

### 記憶モデルの拡張: 正本と認知処理を分離する

2026-09の採用設計。Wave 2の基盤完成やADR更新を、新モデルの実装・運用完了とは扱わない。

| Epic | 実現する機能 |
|---|---|
| #340 | 日常経験・仮定/創作の文脈を含むEpisodeとFactを形成・管理する。同thread照合とFact補足・訂正を行い、devで通常会話への利用まで受け入れる。[要件・受入](epic-340-episodic-memory-requirements.md) |
| #341 | 明示知識の直接抽出と、経験から派生した意味記憶を共通管理する |
| #100 | 保存済み経験から意味知識を一般化し、内省・再内省・派生結果を形成する |
| #354（後続） | 同一キャラクターの別スレッド間Factを、保存後の独立した非同期処理で整理・統合する |

#354は上記3 EpicのMVP必須依存・完了条件にしない。UI・受入は各Epicで確認する。
人格更新#101、Skill学習#102、Life State・会話外活動#249の責務は維持する。
詳細な契約と依存は[記憶境界ADR](decisions/episode-fact-semantic-boundaries-2026-09.md)と各Issueを参照する。

### Wave 3: 「自然に話せる」（LiveKitによる双方向音声会話）

設計判断: `docs/decisions/post-mvp-enhancement-2026-07.md`
計測契約: `docs/voice-quality-measurement.md`

Wave 3の音声機能は最初からLiveKit経路へ実装する。現行WebSocket音声pipelineは
移行前baselineとして凍結し、Wave 3機能を追加しない。実装完了後に別工程でtransportを
切り替える計画は設けない。

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

- 外部MCP接続・実行基盤（#104）を実装。#152の契約に基づきEpic受入を行い、会話利用は#182へ続く。
  詳細は[外部MCP利用基盤](external-mcp-foundation.md)を参照。
- パーソナルAI向けツール連携
- LLMプロバイダの拡張
- クライアント、常時稼働、アバター連携の拡張

### Epic C: 複数キャラクター会話

- User + 光織 + 葵のテキストグループチャット
- 共有会話とCharacter別episodic memoryの分離
- LiveKit Roomへの複数Character音声統合
