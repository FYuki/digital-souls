# Post-MVPエンハンス計画

## この文書について

MVP（テキスト+音声チャット、RAG基盤）完了後の開発を、Wave 1〜4構成で計画する。
`docs/roadmap.md` のpost-MVPセクションに対応する詳細ドキュメント。
背景・決定経緯は `docs/decisions/post-mvp-enhancement-2026-07.md` を参照。

## 背景

### MVP到達点

- テキスト+音声チャット: faster-whisper（STT）→ Ollama（LLM）→ VOICEVOX（TTS）のパイプライン、
  VAD付きマイクUI、WebSocket統合、単一キャラクター「光織」で動作
- RAG基盤: Chroma + SQLite + 記憶ポリシー（`backend/app/memory/`）を実装済み

### 実装済み基盤と残存ギャップ（コード調査で判明）

1. **会話履歴とprompt合成（実装済み）**: SQLiteへ保存した同一キャラクター・同一会話セッションの
   完了済み往復をRAGの有効・無効にかかわらず復元し、Character Card V3、RAG、履歴、
   現在発言、最終指示を`PromptBuilder`で合成する
2. **RAGが眠っている**: 実装済みだが `RAG_ENABLED=false` がデフォルト。長期記憶化も
   明示マーカー（「農業日誌:」等）付き発言のみが対象
3. **スキーマ不整合**: SQLiteは `character` カラムのままで、決定事項
   （`docs/decisions/archive/Multi-character-db-2026-06.md` の初期判断。現行仕様はWave 2 ADRへ統合済み）
   と食い違っている。Chromaはコレクション名分離のみで、メタデータへの `character_id` 付与がない
4. **応答生成が全文待ち**: LLMは `stream:false`（全文生成を待ってから返す）、
   TTSも全文確定後に一括合成するため、体感遅延が大きい
5. **音声がターン形式**: FEのVADが発話終了を検出してPCMを一括送信し、
   BEがSTT/LLM/TTSをすべて完了させてから、ユーザー転写・応答テキスト・音声WAVの3フレームを
   一括で返信する。双方向・割り込み可能な会話にはなっていない
6. **設定のenv化（実装済み）**: Ollamaのモデル・context・応答予約量、Whisperモデル、
   履歴・入出力のtoken上限をtyped settingsへ集約し、Profile経由でもBackendと環境adapterへ
   同じ解決値を伝播する。クラウドLLMの実接続は未実装
7. **Character Card V3のruntime利用（実装済み）**: Character Cardをruntime人格定義の正本とし、
   人格領域、`system_prompt`、`post_history_instructions`をpromptへ反映する。`first_mes`は
   初回assistant表示用として通常promptへ含めず、TTS設定は
   `data.extensions.digital_souls.tts_config`から取得する

## Wave構成の考え方

現状ギャップへの対応を、ユーザー体験の軸で「続く → 覚えている → 自然に話せる → 役に立つ」の
4段階に並べる。各Waveは前段の完了を前提にしないが、実装難易度・ユーザー価値の両面で
この順序が合理的と判断した（詳細は決定ログを参照）。

| Wave | テーマ | 主眼 |
|---|---|---|
| Wave 1 | 会話が「続く」 | 短期記憶・基盤整備。多ターン会話を成立させる |
| Wave 2 | 「覚えている」 | RAG本稼働。旧Phase 5の実質的完遂 |
| Wave 3 | 「自然に話せる」 | 会話状態管理による双方向会話。ターン形式からの脱却 |
| Wave 4 | 「役に立つ」 | ツール・プロバイダ拡張・配信連携。優先度低・後続 |

## Wave 1: 会話が「続く」（短期記憶・基盤整備）

### 1. SQLite会話履歴schema（実装済み）

UI上のスレッドIDは実装上の`conversation_id`へ統一し、conversationとturnをSQLiteへ保存する。
dev／testの既存レコードはテストデータとして扱い、schema変更時のmigrationを保証せず、
現行schemaを空状態から再作成できるものとする。

dogfoodは実conversation historyを保持する運用相当環境である。schema変更前のbackup、対応schema、
migration、検証、rollbackを必須とし、dev／testの破棄契約を適用しない。データ切替の方針は
`docs/decisions/local-dogfood-environment-2026-08.md`と
`docs/decisions/rag-memory-privacy-policy-2026-07.md`を参照する。

### 2. 共通privacy scannerと履歴sanitizer（実装済み）

決定論的privacy scannerは保存先に依存しない型付きfindingを返し、履歴とRAGで再利用する。
APIキー、password、秘密鍵、決済認証、口座番号、政府ID、私用連絡先、正確な住所等は
履歴保存前に値を不可逆placeholderへ置換する。安全にマスクできない場合と明示的な
履歴非保存要求では本文を保存しない。

health、心理状態、金融状況、第三者情報等の話題は同一conversationの履歴には保持できる。
userとassistantへ同じscannerとsanitizerを適用し、原文、原文hash、マスク前本文を永続化しない。
履歴sanitizerはRAG保存可否を返さない。

scannerは`ScanSuccess`またはmetadata-onlyの`ScanFailure`を返し、findingのspanはNFKC等の
正規化viewから原文の半開区間へ復元する。MVPは日本と米国の固定corpusから開始する。
Wave 1で実装した保存拒否findingは`RAG`、`HISTORY`、`BOTH`のscopeを持つ。
Wave 2では短期記憶から長期記憶を形成する方針に合わせ、`HISTORY`を廃止し、
「履歴に残さないで」を`BOTH`へ移行する。この契約変更は#33でscanner、policy、履歴sanitizer、
テストを一緒に更新する。効力はcurrent userのcurrent turnだけとする。

### 3. プロンプト合成の一元設計（実装済み）

次の要素の合成順序・優先順位を`PromptBuilder`へ一元化した。

- Character Card V3の人格領域と`system_prompt`
- RAG記憶（検索結果。RAG本稼働はWave 2の残作業）
- SQLiteへ保存されたマスク済み会話履歴
- 現在ターンのuser原文
- `post_history_instructions`

現在ターンの原文と、永続化済みのマスク済み履歴を型・引数で区別する。
`first_mes`は初回assistant表示用データとして保持し、通常のpromptには含めない。
`personality.md`はCharacter Card編集時の非runtime補助資料とする。

### 4. 会話履歴のプロンプト注入（実装済み）

SQLiteから同じ`character_id`と`conversation_id`の直近N往復だけを復元し、LLMへのpromptに
含める。BE自体はステートレス設計を維持し、状態はSQLiteが持つ形にする。

会話履歴の記録経路はRAGの有効・無効から分離済みであり、`RAG_ENABLED=false`でも記録する。

RAGを切った状態でも、直前のやり取りを踏まえた応答を生成できる。

### 5. 会話ライフサイクルとスレッド管理（実装済み）

HTTPとWebSocketで同じ`character_id` / `conversation_id`、状態遷移、privacy処理順序を使用する。
Frontendはcharacter単位のconversation IDを保持し、スレッド一覧、再開、アーカイブ、復元、
物理削除を提供する。アーカイブは履歴を保持したまま通常一覧から非表示にし、物理削除は
conversationとturnをSQLiteからhard deleteする。
別conversationの生履歴を横断検索しない。

### 6. 設定のenv化（実装済み）

- `OLLAMA_CHAT_MODEL`、`OLLAMA_CONTEXT_TOKENS`、`OLLAMA_RESPONSE_RESERVE_TOKENS`で
  Ollamaのモデル、実行時context、応答予約量を設定する
- `WHISPER_MODEL`で実行時モデルとcache準備対象を揃える
- 会話履歴件数、履歴token上限、user入力上限、assistant最大生成量、モデル最大contextを設定する
- typed settingsで不正値と設定間の不整合を起動時に拒否し、Profile経由でBackendと環境adapterへ
  同じ解決値を伝播する

モデル差し替え・チューニングをコード変更なしで行える。

## Wave 2先行基盤: dogfood環境分離

親Issue: #50

TAKTによる開発と安定版の継続利用を並行するため、#22の再開前提としてdogfood環境を分離した。
Issue #50と子Issue #51〜#56は2026-08-17に手動受入まで完了しており、#22をcleanなmainから再開できる。

- [x] #52 runtime data rootと環境identity
- [x] #51 managedサービスのport分離とdogfood Profile
- [x] #53 Ubuntu-dogfoodと共通推論サービス
- [x] #54 deploy、rollback、常駐運用
- [x] #55 backup、restore
- [x] #56 TAKTとの並行稼働・データ分離受入

Wave 2親Issue #28の受入まではdogfoodのRAGを無効にし、旧Chromaデータを作らず、
conversation historyだけを実データとして保持する。
dogfoodのSQLite schema変更へdev／testの「削除して再作成」を適用しない。

## Wave 2: 「覚えている」（RAG本稼働 = 旧Phase 5の実質的完遂）

Wave 2の設計詳細はこの計画書で重複管理しない。現行の設計上の正本は次とする。

- `docs/decisions/wave2-memory-formation-retrieval-2026-08.md` — 記憶／記録のモデル、形成、検索、評価
- `docs/decisions/rag-memory-privacy-policy-2026-07.md` — Wave 1から継続するprivacy不変条件
- 親Issue #28 — 子Issueの進捗、依存順、最終受入条件

実装は次の依存順で進める。

```text
#25（完了）
  -> #50（dogfood環境分離、完了）
  -> #22
  -> #33
  -> #8
  -> (#29 || #30)
  -> #31
  -> #9
  -> #10
  -> (#11 || #12)
  -> #28の受入確認
```

Wave 2で達成する成果は次に限定する。

- 会話履歴、人格記憶、domain record、検索機構の責務を分離する
- `SemanticPrivacyClassifier`は文脈だけを分類し、決定論的な`RagAdmissionEvaluator`が保存を決める
- allowlistを保存同意として扱い、個別確認・保存通知なしで安全な構造化候補だけを非同期保存する
- conversation由来の長期記憶は、元turnの履歴本文が保存済みの場合だけ形成する
- SQLiteを正本、Chromaを派生indexとし、outboxとreconciliationで収束させる
- 機微なcurrent queryではRAG検索をskipする
- 検索順位は意味的関連度を主、`last_user_mentioned_at`を同等関連度内のtie-breakとする
- access count、時間減衰、固定複合重みはMVPへ入れない
- ユーザーが人格記憶と暫定domain recordを一覧、訂正、物理削除できるようにする

各IssueにはそのIssue固有の入出力、テスト、完了条件だけを記載し、共通仕様はADRを参照する。
idle時のpersona memory consolidationはWave 2受入後の#48で行い、親#28の完了条件には含めない。

## Wave 3: 「自然に話せる」（LiveKitによる双方向音声会話）

現状のターン形式（Frontend VAD検出→発話単位PCMをWebSocket送信→Backend一括処理→
turn JSONと単一WAVを返信）から、LiveKit / WebRTC上の継続音声sessionへ移行する。
LiveKit採用は決定済みであり、現行WebSocketは変更前baselineの計測対象に限定する。

### 責務境界

- LiveKitはRoom、Participant、Track、WebRTC media、再接続を担当する。
- Frontendはmicrophoneを継続publishし、VAD検出主体として
  `speech_started` / `speech_stopped` eventを通知する。
- Conversation Coreはspeech eventのcontract、utterance確定、`should_response`、response開始、
  cancel、STT、LLM、VOICEVOX、履歴、privacy、記憶の意味論を管理する。
- LiveKit固有identityと `session_id` / `utterance_id` / `response_id` を分離する。
- mediaはLiveKit AudioTrack、response deltaやcancel等のcontrol eventはtransport非依存contractとし、
  LiveKitのdata/RPC等へmappingする。

### 独立したlifecycleと世代管理

voice session、user utterance、assistant response、client playbackを独立して管理する。
listeningとspeakingは同時に成立でき、`speech_stopped`、utterance確定、`should_response`、
response開始を同一eventとして扱わない。

responseにはIDとsequenceを付け、generation、synthesis、deliveryを途中cancelできるようにする。
cancel済みresponseの遅延text、control event、audioは世代gateで破棄し、完了・中断・失敗を
履歴上で区別する。中断・失敗したresponseから長期記憶を形成しない。

### 継続入力と漸進的応答

音声session中はAIの思考・発話状態にかかわらずmicrophone trackとFrontend VADを継続する。
Ollamaの生成deltaを逐次表示し、意味のある文・節が確定するごとにVOICEVOXで合成して
Character AudioTrackへ順序どおりpublishする。生成、合成、publishの速度差にはqueue上限と
backpressureを設ける。

### barge-in（割り込み対応）

Character発話中にFrontend VADが利用者のspeech startを検出した場合、server round tripを待たず
local再生を停止し、active responseを指定してConversation Coreへcancelを通知する。
割り込み発話は新しいutteranceとして冒頭から扱い、最新意図を優先する。

browser echo cancellationとnoise suppressionを維持し、speaker音による自己割り込みを抑制する。

### LiveKit接続と回復

Room作成、join token、Participant identity、Track publish / subscribeの境界を設ける。
再接続後は古いsession / responseを復活させず、track再購読による二重再生を防ぐ。
STT、LLM、TTS、audio publish / playbackの一時障害後も次の発話を処理できる状態へ収束させる。

### 計測と受入

最初に現行WebSocket一括pipelineのbaselineを取得し、同じ指標でLiveKit版Wave 3を評価する。
計測は採否判断ではなく、TTFA、barge-in停止・cancel遅延、冒頭欠落、stale出力、再接続、
audio delivery gapの受入目標を定義するために行う。自動受入後、実LiveKit、Whisper、Ollama、
VOICEVOX、browser microphone / speakerを使ってdogfood受入する。

### 依存関係

現行WebSocket baseline取得を開始した後、transport非依存contractとLiveKit基盤設計を並行する。
その後、response世代管理、継続入力、中断履歴を基盤として、逐次text、逐次audio、barge-in、
再接続・障害回復、自動受入、dogfood受入の順に進める。詳細な進捗と完了条件はGitHub Issuesで管理する。

## Wave 4: 「役に立つ」（後続・優先度低）

優先度は低いが、旧Phase 6〜8のタスクをここに集約する。

1. ツール実行基盤 + 農業日誌
2. `ClaudeClient` 実装・プロバイダ切替（現状は `NotImplementedError` スタブ）
3. Discord Bot / Mac mini常時稼働 / Live2D

複数キャラクター会話はEpic CとしてWave 3・4から分離し、テキストグループチャット、
Character別episodic memory、LiveKit複数Character音声統合の順に進める。

## 旧Phase → Wave 対応表

| 旧Phase | 内容 | 移行先 |
|---|---|---|
| Phase 4（未完了分） | WebSocketの遅延baseline取得・LiveKit対応 | Wave 3（LiveKit採用済み） |
| Phase 5 | 長期記憶（RAG） | Wave 2 |
| Phase 6 | パーソナルAI機能（農業日誌・レシピ管理等） | Wave 4 |
| Phase 7 | 表現・配信連携（Live2D・VRM等） | Wave 4 |
| Phase 8 | 常時稼働化・マルチクライアント対応 | Wave 4 |

Wave 1・Wave 3の会話状態管理部分は、コード調査で新たに判明したギャップに基づく新規タスクであり、
旧Phaseには対応項目がない。
