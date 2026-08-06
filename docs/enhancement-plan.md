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
開発環境が検証環境を兼ねる間、既存レコードはテストデータとして扱い、schema変更時の
データmigrationは保証せず、現行schemaを空状態から再作成できるものとする。
実データを保持する運用へ移行する際に、backup、対応schema、rollbackを含むmigration方針を
その時点のschemaに基づいて決定する。データ切替の方針は
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

## Wave 2: 「覚えている」（RAG本稼働 = 旧Phase 5の実質的完遂）

Wave 2の設計詳細はこの計画書で重複管理しない。現行の設計上の正本は次とする。

- `docs/decisions/wave2-memory-formation-retrieval-2026-08.md` — 記憶／記録のモデル、形成、検索、評価
- `docs/decisions/rag-memory-privacy-policy-2026-07.md` — Wave 1から継続するprivacy不変条件
- 親Issue #28 — 子Issueの進捗、依存順、最終受入条件

実装は次の依存順で進める。

```text
#25（完了）
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
