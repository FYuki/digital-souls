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

### 現状ギャップ（コード調査で判明）

1. **会話が完全ステートレス**: 直前のやりとりすらプロンプトに含まれず、多ターン会話が成立しない。
   プロンプトは personality.md +（RAG有効時のみ）検索記憶 + 今回の発言のみで構成される
2. **RAGが眠っている**: 実装済みだが `RAG_ENABLED=false` がデフォルト。長期記憶化も
   明示マーカー（「農業日誌:」等）付き発言のみが対象
3. **スキーマ不整合**: SQLiteは `character` カラムのままで、決定事項
   （`docs/decisions/Multi-character-db-2026-06.md` の「全レコードに `character_id` を付与する」）
   と食い違っている。Chromaはコレクション名分離のみで、メタデータへの `character_id` 付与がない
4. **応答生成が全文待ち**: LLMは `stream:false`（全文生成を待ってから返す）、
   TTSも全文確定後に一括合成するため、体感遅延が大きい
5. **音声がターン形式**: FEのVADが発話終了を検出してPCMを一括送信し、
   BEがSTT/LLM/TTSをすべて完了させてから、ユーザー転写・応答テキスト・音声WAVの3フレームを
   一括で返信する。双方向・割り込み可能な会話にはなっていない
6. **設定のハードコード**: モデル名 `gemma4:e4b` が `backend/app/llm/ollama_client.py` に
   直書きされており、`ClaudeClient` は `NotImplementedError` のスタブのまま
7. **card.jsonの未使用フィールド**: `system_prompt` / `first_mes` / `post_history_instructions` が
   未使用で、`tts_config` のみが参照されている

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

### 1. SQLite会話履歴schema

既存SQLiteはテストデータのためmigrationせず削除し、`conversations`と`conversation_turns`を
空状態から作成する。UI上のスレッドIDは実装上の`conversation_id`へ統一する。

### 2. 共通privacy scannerと履歴sanitizer

決定論的privacy scannerは保存先に依存しない型付きfindingを返し、履歴とRAGで再利用する。
APIキー、password、秘密鍵、決済認証、口座番号、政府ID、私用連絡先、正確な住所等は
履歴保存前に値を不可逆placeholderへ置換する。安全にマスクできない場合と明示的な
履歴非保存要求では本文を保存しない。

health、心理状態、金融状況、第三者情報等の話題は同一conversationの履歴には保持できる。
userとassistantへ同じscannerとsanitizerを適用し、原文、原文hash、マスク前本文を永続化しない。
履歴sanitizerはRAG保存可否を返さない。

### 3. プロンプト合成の一元設計

以下の要素の合成順序・優先順位を確定し、一元化する。

- personality.md（人格設定）
- card.json の未使用フィールド（`system_prompt` / `first_mes` / `post_history_instructions`）
- 会話履歴（Wave 1-4で追加）
- RAG記憶（検索結果、Wave 2で本稼働）

現在ターンの原文と、永続化済みのマスク済み履歴を型・引数で区別する。
現状はプロンプト構築ロジックが分散している想定のため、単一のプロンプトビルダーに集約する。

### 4. 会話履歴のプロンプト注入

SQLiteから同じ`character_id`と`conversation_id`の直近N往復だけを復元し、LLMへのpromptに
含める。BE自体はステートレス設計を維持し、状態はSQLiteが持つ形にする。

RAGが無効（`RAG_ENABLED=false`）の状態でも会話ログは常時記録されるよう、
記録経路をRAGの有効/無効から分離する（現状はRAG有効時のみ記録に寄っている可能性があるため要確認）。

完了イメージ: RAGを切った状態でも、直前のやり取りを踏まえた応答が返る。

### 5. 会話ライフサイクルとスレッド管理

HTTPとWebSocketで同じ`character_id` / `conversation_id`、状態遷移、privacy処理順序を使用する。
Frontendはcharacter単位のconversation IDを保持し、スレッド一覧、再開、削除を提供する。
別conversationの生履歴を横断検索しない。

### 6. 設定のenv化

- `OLLAMA_CHAT_MODEL` 等、`gemma4:e4b` のハードコードを解消する
- Whisperモデルサイズを環境変数化する
- 履歴注入数N（Wave 1-4で使用）を環境変数化する

完了イメージ: モデル差し替え・チューニングがコード変更なしで可能になる。

## Wave 2: 「覚えている」（RAG本稼働 = 旧Phase 5の実質的完遂）

### 1. health意味分類基盤

決定論的health screenerと実装交換可能な意味分類器を組み合わせる。ローカルLLM、
spaCy／GiNZA等のparser、専用分類器を候補とし、日英の固定conformance corpusで検出品質、
過検知、初期化時間、判定時間、メモリ使用量を比較する。MVPでは1方式を選定する。

分類結果は保存許可と分離し、カテゴリ、本人・第三者・一般の対象、判定結果、reason code、
実装versionを型付きで返す。SQLiteやChromaへ書き込まず、RAG admissionへassessmentを渡す。

### 2. RAG admission policy

Wave 1の共通privacy scannerとhealth assessmentを再利用する。絶対禁止scanner、保存拒否指示、
positive allowlist型の候補抽出、候補全体の機微情報再検査、必要なユーザー確認を順に行う。
scannerは保存可否を決めず、決定的なapplication policy evaluatorだけが
`RagAdmissionDecision`を返す。秘密値をマスクした候補をRAG保存へ昇格させない。

### 3. SQLiteを長期記憶の正本にする

- `approved_memories`へ許可型、正規化本文、`character_id`、`source_conversation_id`、
  `policy_version`、状態、日時を保存する
- SQLiteを訂正・削除・policy状態の正本とする
- 既存のSQLite／Chromaテストデータは移行せず、空状態から開始する
- `docs/decisions/Multi-character-db-2026-06.md`の決定事項どおり、全レコードを
  `character_id`で分離する

### 4. transactional outboxとChroma派生index

- `approved_memories`保存と`memory_index_outbox`作成を同じSQLite transactionで行う
- Chromaへ`memory_id`単位で冪等にupsert／deleteする
- outbox・application log・fallbackファイルへ本文やembeddingを複製しない
- 旧`failed-memories.jsonl`を廃止する

### 5. ingestion時・取得時のprivacy境界

- ingestion時は共通scanner、意味分類、許可型抽出、ユーザー確認、policy evaluatorを通す
- `ALLOW_STRUCTURED`だけを`approved_memories`とoutboxへ保存する
- 取得時はChromaの`memory_id`をSQLiteで引き直し、`character_id`、状態、TTL、
  `policy_version`を検証し、SQLite本文へ決定論的絶対禁止scannerを再適用する
- 一般質問が機微情報でないことを、RAG保存許可の根拠にしない

### 6. RAG検索品質検証 → デフォルト有効化

- 検証セットを用意し、無関係な記憶がプロンプトを汚染しないかを確認する
- Embeddingモデル・チャンク戦略を必要に応じて調整する
- 問題ないことを確認した上で `RAG_ENABLED=true` をデフォルトにする

### 7. positive allowlistと自動記憶昇格

- 会話サマリから許可型の長期記憶候補を生成する
- `docs/decisions/miori-memory-policy-2026-06.md` の「重要な長期記憶は原則確認してから保存する」方針に沿い、
  光織がユーザーに確認してから保存するフローを実装する
- 明示的な「覚えて」は確認済み候補として扱うが、絶対禁止と機微情報判定は省略しない
- 未確認候補はRAGへ保存しない
- 現状の「明示マーカー付き発言のみ長期記憶化」という制約を緩和する

### 8. 時系列照合

- 記憶に日付メタデータを付与し、時期指定での検索を可能にする
- 「昨年もこの時期に〜」のような応答を実現する
- personality.md が描く長期パートナー性の中核体験にあたる機能

### 9. 記憶の閲覧・削除インターフェース

- `docs/decisions/miori-memory-policy-2026-06.md` の「削除・訂正依頼は優先対応する」方針の実装担保
- ユーザーが記憶内容を閲覧・削除・訂正できるインターフェースを用意する

## Wave 3: 「自然に話せる」（会話状態管理による双方向会話）

現状のターン形式（FE VAD検出→一括送信→BE一括処理→3フレーム一括返信）から、
状態管理された双方向会話へ移行する。

### 会話状態マシン

セッション単位で以下の状態を管理する。BE側が正とし、状態変化のたびにFEへ状態フレームで通知する。
FE側UIも状態表示に対応させる。

```text
idle      : 待機中
listening : ユーザー発話を受信中
thinking  : LLM生成中
speaking  : 応答音声を再生中
```

### WSプロトコル拡張

既存のフレームに加えて以下を追加する。既存フレームとの互換方針（バージョニング、フォールバック）も
実装時に確定する。

| フレーム | 方向 | 用途 |
|---|---|---|
| `{type:"state"}` | BE→FE | 状態遷移の通知 |
| `{type:"text_delta"}` | BE→FE | LLM応答のストリーミング差分 |
| `{type:"audio_chunk"}` | BE→FE | 文単位で合成された音声チャンク |
| `{type:"audio_end"}` | BE→FE | 音声送出の終端通知 |
| `{type:"cancel"}` | FE→BE | ユーザーによる割り込み（barge-in）通知 |

### LLMストリーミング

Ollamaを `stream:true` で呼び出し、生成デルタを `text_delta` フレームでFEへ逐次送信する。
FE側は受信したデルタを逐次表示する。

### 文単位ストリーミングTTS

文が確定するごとにVOICEVOXで合成し、`audio_chunk` として逐次送出・再生する。
`speaking` 状態をさらに細分化し、途中で割り込み可能にする。

### barge-in（割り込み対応）

`speaking` 中にユーザー発話を検出した場合、以下を行う。

1. 再生中の音声を停止する
2. BE側の生成処理をキャンセルする（`cancel` フレーム受信）
3. `listening` 状態へ遷移する

エコー対策（`echoCancellation` の有効化、`speaking` 中のVAD制御）を課題として明記する。
マイク入力が自身の再生音声を拾って誤発火しないようにする検討が必要。

### 遅延計測の指標化 → LiveKit移行判断

旧Phase 4の残タスク「WebSocketの遅延を計測し、LiveKit移行の必要性を判断する」をここへ移動する。
新プロトコル（状態マシン・ストリーミング）を前提に遅延を計測し、その上でLiveKit移行を判断する。
FE側の `AudioTransport` 抽象化（`frontend/src/lib/audio/transport.ts`）は、
どちらの判断になっても差し替えられるよう温存する。

### 既存の音声1件保持キューの再設計

`backend/app/routers/ws.py` に実装済みの処理中キュー（1件保持・上書き）は、
状態マシン導入に合わせて再設計する（`listening`/`thinking` 状態との整合を取る）。

### 依存関係

LLMストリーミングと文単位TTSは、会話状態マシン・WSプロトコル拡張の設計が確定した後に着手する
（フレーム種別・状態遷移が先に決まっていないと、ストリーミング実装がやり直しになるため）。

## Wave 4: 「役に立つ」（後続・優先度低）

優先度は低いが、旧Phase 6〜8のタスクをここに集約する。

1. ツール実行基盤 + 農業日誌
2. `ClaudeClient` 実装・プロバイダ切替（現状は `NotImplementedError` スタブ）
3. 2人目キャラクターでの複数キャラクター運用検証
4. Discord Bot / Mac mini常時稼働 / Live2D

## 旧Phase → Wave 対応表

| 旧Phase | 内容 | 移行先 |
|---|---|---|
| Phase 4（未完了分） | WebSocketの遅延計測・LiveKit移行判断 | Wave 3 |
| Phase 5 | 長期記憶（RAG） | Wave 2 |
| Phase 6 | パーソナルAI機能（農業日誌・レシピ管理等） | Wave 4 |
| Phase 7 | 表現・配信連携（Live2D・VRM等） | Wave 4 |
| Phase 8 | 常時稼働化・マルチクライアント対応 | Wave 4 |

Wave 1・Wave 3の会話状態管理部分は、コード調査で新たに判明したギャップに基づく新規タスクであり、
旧Phaseには対応項目がない。
