# 開発ロードマップ

## 目的

`digital-souls`の開発を、キャラクター設計・会話基盤・音声・長期記憶・外部活動・表示連携へ段階的に進める。

この文書はPhase／Wave等で実現する機能を示す。タスクの進捗、依存関係、完了条件はGitHub Issuesで管理し、チェックボックスや個別Issueの状態を重複管理しない。
現在のmainの実装範囲は[README](../README.md)と[アーキテクチャ](system-architecture.md)、用語と設計／実装の区別は[用語集](glossary.md)を参照する。

2026-06-17にAIRIフォーク利用を取りやめ、自作BE（FastAPI）+ 自作FE（Vite + Svelte）へ移行した。
2026-07-09にはMVP完了を受け、旧Phase 5〜8のタスク列挙をpost-MVPのWave 1〜4へ再編した。
経緯は[post-MVP ADR](decisions/post-mvp-enhancement-2026-07.md)、詳細な機能分解は[拡張計画](enhancement-plan.md)を参照する。

## Phase 0〜4: MVP（完了）

以下は完了済みの履歴として残す。個別の経緯は`docs/decisions/`を参照する。

### Phase 0: 方針整理

- リポジトリ構成とGitHub運用方針。
- 自作Backend／Frontendを中核とするアーキテクチャ。
- Live2D、VRM、静止画UI、Mac mini、Windows、Cloud VMの役割分担の方針。

### Phase 1: キャラクター設計

- 光織の性格、世界観、記憶方針。
- 複数キャラクターに対応する`characters/`構成。

### Phase 2: 開発環境整備

- Windows + WSL2、Docker利用方針、ローカルLLMの開発・検証環境。
- Mac miniへの移行方針。

### Phase 3: テキストチャット基盤

- 自作Backend／Frontendのテキスト会話。
- キャラクターを指定した会話UI。

### Phase 4: 音声対応

- ブラウザ音声会話、STT／TTSの基盤。
- テキスト会話と音声会話の統合。

Phase 4で残った音声遅延baselineとLiveKit移行はWave 3へ、MVPで構築したRAG基盤の本稼働化はWave 2へ移した。

## Post-MVP: Wave 1〜4

MVPで判明した多ターン会話、RAG本稼働、応答遅延等の課題を、「続く → 覚えている → 自然に話せる → 役に立つ」で整理する。
各Waveの設計詳細は[拡張計画](enhancement-plan.md)とADR、作業管理はGitHub Issuesを参照する。

### Wave 1: 会話が「続く」（短期記憶・基盤整備）

- 会話履歴とスレッドの永続化、privacy保護、保存済み履歴による多ターン会話。
- 実行時設定の外部化、FE/BEで統一された会話ライフサイクル。
- スレッドの一覧、再開、アーカイブ、復元、削除。

### Wave 2: 「覚えている」（RAG本稼働）

設計上の正本は[Wave 2 ADR](decisions/wave2-memory-formation-retrieval-2026-08.md)。記憶・人格の拡張設計との優先関係は[ADR案内](decisions/README.md)を参照する。

- 文脈依存の機微情報判定とpositive allowlistによる保存判断。
- SQLite正本／Chroma派生index、保存済み会話履歴からの非同期形成、機微なqueryで検索を抑止する境界。
- 検索品質評価、時系列照合、人格記憶・暫定記録の閲覧・訂正・物理削除。
- idle時の既存記憶consolidation、devとdogfoodのruntime data・service・backup・deploy分離。

### Wave 3: 「自然に話せる」（LiveKitによる双方向音声会話）

設計判断は[post-MVP ADR](decisions/post-mvp-enhancement-2026-07.md)、通信境界は[LiveKit ADR](decisions/livekit-transport-2026-08.md)、計測契約は[音声品質計測](voice-quality-measurement.md)を参照する。

LiveKitを正式な音声経路とし、旧WebSocket音声pipelineは移行前baselineとして凍結する。旧経路へ新機能を追加してから切り替える二段階実装にはしない。

- Room・Participant・Track・接続認証と、transport非依存のSession・Utterance・Response・Playback lifecycle。
- 継続microphone入力とVAD、LLMテキスト逐次配信、VOICEVOX逐次合成、Character AudioTrack再生。
- response単位の世代管理・cancel・遅延出力破棄、barge-inと最新入力優先、中断・失敗・完了を区別する履歴・記憶整合性。
- Session状態UI、再接続、障害回復、遅延・割り込み・再接続の計測と自動／dogfood受入。

Speech/Textの併用、入力受理照合、focus抑止、スレッド別送信は[混在Session契約](decisions/conversation-session-text-input-2026-09.md)と[共通client契約](decisions/conversation-session-client-2026-09.md)へ続く。
実装があることと品質・長時間運転の受入完了は別で、結果・残課題は[受入記録](conversation-session-acceptance.md)と関連運用記録で追跡する。

TTS差し替えの拡張設計は[Irodori要件](irodori-tts-requirements.md)と[Epic #329](https://github.com/FYuki/digital-souls/issues/329)を参照する。既存の区間合成を再利用し、実装・性能受入・モデル最終採用は今後の検証対象とする。本採用音声の選定は独立した関連[Issue #330](https://github.com/FYuki/digital-souls/issues/330)で扱う。

### Wave 4: 「役に立つ」（外部ツール・推論・利用形態の拡張）

外部MCPを将来の接続候補としてだけ扱わず、既存の接続・実行・会話利用・管理基盤を起点に拡張する。

- 外部MCP接続、Capability Snapshot、Execution Gate、Resource利用：[MCP基盤](external-mcp-foundation.md)。
- 会話からのツール選択・不足情報確認・結果統合：[会話からの利用](tool-use.md)。
- 接続管理、操作承認・実行確認・結果回復：[Addon管理](addon-admin.md)と[承認・回復ADR](decisions/addon-action-approval-recovery-2026-09.md)。
- 用途別Inference TargetとProvider Adapter：[Inference運用](inference-operations.md)。
- 画面知覚、追加クライアント、常時稼働、アバター連携：[画面知覚ADR](decisions/browser-screen-perception-2026-09.md)と[拡張計画](enhancement-plan.md)。

個別の業務addon、self-owned runtime、Desktop／Discord、Live2D／VRM等まで一括して実装済みとしない。

### Character Life: 会話外の活動・記憶・人格

複数領域にまたがる設計は[Character Life共通契約](decisions/character-life-memory-personality-autonomy-2026-09.md)と[用語契約](decisions/memory-personality-terminology-2026-09.md)に従う。

- DBOSによる活動・Life State・許可・実行管理の基盤と、関連domainの正本を分離する。
- SELF経験、意味抽象化、内省、興味関心、人格適応、手続き学習をそれぞれの責務として接続する。
- 忘却を検索・想起しにくさとして扱う検討は、必要性が確認できた段階で詳細化する。

現在の有効化条件と未接続portは[Character Life運用](character-life-operations.md)を参照する。DBOS基盤の導入を全体シナリオの完成とは扱わない。

### Epic C: 複数キャラクター会話

- Userと複数キャラクターのテキストグループチャット。
- 共有会話とCharacter別episodic memoryの分離。
- LiveKit Roomへの複数Character音声統合。
