# システムアーキテクチャ

> **2026-06-17 方針転換**: AIRIフォーク利用を取りやめ、自作BE（FastAPI）+ 自作FE（Vite + Svelte）構成に移行した。
> 「AIRIの位置づけ」セクションは本転換に伴い失効しているため削除し、現行の自作構成の記述に置換した。
> 理由・経緯は `docs/decisions/` を参照。

## 基本思想

`digital-souls` では、AI人格の本体を「表示・配信システム」ではなく、「人格・記憶・判断・ツール実行」に置く。

表示形態は用途に応じて切り替える。

- 日常利用: 静止画UIまたは軽量チャットUI
- 通常の視覚表現: Live2D
- 配信・イベント時: 必要に応じてVRM
- 重い推論: WindowsメインPCまたはCloud VM

## 全体構成

```text
                     User / Viewer
                          │
                          ▼
                  Input Interface
          Chat / Voice / Discord / Web UI
                          │
                          ▼
                 digital-souls Core
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
        ▼                 ▼                 ▼
   Personality         Memory             Tools
 characters/        RAG / DB        Farming / Recipe
        │                 │                 │
        └─────────────────┼─────────────────┘
                          ▼
                  Inference Router
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
        ▼                 ▼                 ▼
   Local LLM         Windows PC        Cloud GPU/VM
 Mac mini/Ollama     Heavy models      Fallback worker
                          │
                          ▼
                 Output Controller
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
        ▼                 ▼                 ▼
   Static Image          Live2D              VRM
  Personal UI       VTube Studio      3tene/Warudo/etc.
```

## 自作BE/FE構成

`digital-souls` のCoreは、自作BE（FastAPI）+ 自作FE（Vite + Svelte）で実装する。

### バックエンド（FastAPI, `backend/app/`）

* `routers/chat.py` — テキストチャットのHTTPエンドポイント
* `routers/ws.py` — 音声チャット用WebSocketエンドポイント（STT→LLM→TTSの一連の処理、音声フレームの処理中キューを含む）
* `chat_service.py` / `_chat_runtime.py` — チャットセッションの生成・応答生成のエントリポイント
* `characters/loader.py` — `characters/` 配下のCharacter Card V3の検証・ロードと、`extensions.digital_souls`の型付き設定読み取り
* `prompting/` — Character Card、RAG、保存済み履歴、現在発言を順序とtoken budgetに従って合成する単一境界
* `llm/` — 完成済みpromptを受け取るLLM振り分けルーターとクライアント実装。`ollama_client.py`（ローカルOllama、常用）、`base.py`（クライアント共通インターフェース）。クラウドLLM（Claude等）向けクライアントは未実装のスタブ
* `memory/` — 会話履歴と長期記憶の基盤。SQLiteに同一conversation再開用の履歴と承認済み長期記憶を責務分離して保存し、Chromaは承認済み長期記憶だけの派生検索インデックスとして扱う。`memory_policy.py`は`backend/app/memory/memory_policy.json`の認識設定と、アプリケーションの非緩和policyを組み合わせて保存先別に判定する
* `stt/whisper_client.py` — faster-whisperによる音声認識
* `model_settings.py` — Ollamaモデル・実行時context・応答予約量、Whisperモデル、履歴・入力・モデルcontext上限を環境変数から型付きで一括解決する。Backendはlifespanの先頭で検証し、不正設定ではリクエスト受付前に起動失敗する
* `tts/voicevox_client.py` / `tts/speech_synthesizer.py` — VOICEVOXによる音声合成
* `audio/transport.py` / `audio_pipeline.py` — 音声フレームの送受信・パイプライン制御

### フロントエンド（Vite + Svelte, `frontend/src/`）

* `lib/audio/transport.ts` — 現行WebSocket通信を抽象化する `AudioTransport`。現在はturn、audio、error、open、close callbackと発話単位audio送信を提供する
* `lib/audio/pcm-worklet-recorder.ts` / `lib/audio/vad-assets.ts` — AudioWorkletによるPCM録音とVAD（発話区間検出）
* `lib/AudioRecorder.svelte` / `lib/AudioPlayer.svelte` — マイク入力UI・音声再生UI
* `lib/ChatWindow.svelte` / `lib/InputBar.svelte` — テキストチャットUI
* `App.svelte` — テキスト/音声チャットを統合したメインUI

## 表示・配信レイヤー

### 基本

* Live2Dを標準の姿とする
* パーソナルAI用途では静止画UIも許容する
* VRMは配信時や3D表現が必要な場合のみ利用する

### Live2D

候補:

* VTube Studio
* OBS連携
* 将来的なAPI制御

### VRM

候補:

* 3tene
* Warudo
* VNyan
* VSeeFace
* Unity + UniVRM
* Three.js + three-vrm

VRMは常用ではなく、配信・イベント用の身体として扱う。

## 推論ルーター

推論処理は用途に応じて振り分ける。

```text
small:
  provider: local
  target: Mac mini / Ollama
  purpose: 日常会話、記録、軽い相談

medium:
  provider: windows
  target: WindowsメインPC
  purpose: 高精度回答、長文推論、重めの処理

large:
  provider: cloud
  target: Cloud GPU/VM
  purpose: Windows未起動時の代替、大規模推論
```

## 音声処理設計

現在の `WhisperTranscriber` は、単一のWhisperモデルインスタンスに対する `transcribe()` 呼び出しをロックで直列化する。想定同時接続ユーザー数は3程度とし、この前提で直列化によるスループット低下を許容する。

同時接続ユーザー数が増加した場合は、モデルインスタンスをプール化する設計への切り替えを再検討する。

## 記憶・ツール設計

### 会話履歴とRAG長期記憶

UI上のスレッドはBackendの`conversation_id`に対応する。同じ`character_id`と
`conversation_id`の履歴だけを復元し、別conversationの生会話は検索しない。

会話履歴を短期記憶、`approved_memories`を人格の長期記憶として扱う。conversation由来の
長期記憶は保存済み会話履歴からだけ形成する。農業日誌やレシピ等の正確なdomain recordは
人格記憶へ混在させず、暫定providerまたはaddon DBが所有する。

```text
受信した会話
  └─ Wave 1: 共通の決定論的privacy scanner
       ├─ 現在ターンの応答生成（原文は処理中だけ利用）
       └─ 履歴用policy + assistant応答のsanitizer
            ├─ SKIP_CONTENT / privacy_skipped
            └─ MASK / STORE
                 └─ SQLite: completedなconversation_turns
                      └─ 非同期の長期記憶形成
                           └─ Wave 2: 文脈依存PrivacyAssessment
                                └─ RAG admission policy
                                     └─ ALLOW_STRUCTURED
                                          └─ SQLite: approved_memories + memory_index_outbox
                                               └─ Chroma: 承認済み記憶の派生index
```

共通privacy scannerは保存先を決めず、カテゴリ、原文上の半開区間、reason code、version、
保存拒否scopeを型付きfindingとして処理中だけ返す。公開interfaceは`ScanSuccess`または
metadata-onlyの`ScanFailure`を返す。NFKC等の認識用viewと原文spanの対応はscanner内部だけで
保持し、MVPは日本と米国の固定corpusから開始する。履歴用policyは、APIキー、password、秘密鍵、決済認証、
口座番号、政府ID、私用連絡先、正確な住所等の値をマスクし、明示的な履歴非保存要求または
安全にマスクできない場合は本文を破棄する。health、心理状態、金融状況、第三者情報等の話題は
同一conversationの履歴として保持できるが、MVPではRAG長期記憶へ昇格させない。
userとassistantの双方へ同じscannerとsanitizerを適用し、原文、検出値、マスク前本文を
SQLiteやapplication logへ残さない。

保存拒否findingはMVPでは`RAG`または`BOTH`のscopeを持ち、current userのcurrent turnだけへ
適用する。「履歴に残さないで」は履歴だけでなくRAG記憶形成も拒否する`BOTH`として扱う。
assistant側で`SKIP_CONTENT`になった場合は、保存済みuser本文も同一transactionで消去し、
turn全体を`privacy_skipped`へ遷移する。

文脈依存`PrivacyAssessment`はWave 2でhealth、心理状態、自傷、虐待・性的被害、金融状況、
第三者の非公開情報、暗示的な機微情報を分類する。classifierは保存可否を返さず、
RAG admission evaluatorだけが決定論的findingとassessmentから保存可否を決める。

conversationのアーカイブは履歴をSQLiteへ保持したまま通常一覧、prompt注入、追記対象から
除外する。物理削除はconversationとturnをSQLiteからhard deleteし、RAG長期記憶は暗黙削除しない。

会話履歴DBの現行schema versionは3である。SQLiteを正本、Chromaを再構築可能な派生indexとし、
backup artifactにはSQLiteと検証用JSONだけを含める。WAL稼働中のbackupはSQLite公式backup APIで
整合snapshotを作成する。restoreはchecksum、schema、environment identityを切替前に検証し、
検証済みstaging SQLiteを単一のatomic置換で切り替える。通常の手動restoreでは、切替前の検証・
置換失敗時に既存DBを維持し、自動rollbackは行わない。dogfood起動時のschema migration失敗では
直前に作成・検証したbackupへ自動rollbackし、rollback自体も失敗した場合はmigrationとrollbackの
両方の失敗を保持して起動を中止する。その他の復旧操作は`infra/dogfood/README.md`の手動restore
手順に従う。
dogfoodのdeployとschema migrationは
事前backupの成功を後続処理の開始条件とする。操作手順とIssue #56のrestore drillは
`infra/dogfood/README.md`を正本とする。

RAG長期記憶はpositive allowlist方式とし、allowlistを保存同意として扱う。許可型へ正規化され、
機微情報検査を通過し、current turnに保存拒否がない`ApprovedMemoryCandidate`だけをSQLiteへ
自動保存する。候補ごとの確認と保存通知は行わない。SQLiteを正本、Chromaを派生indexとし、
conversation由来の候補は元turnの履歴本文が保存済みの場合だけ長期記憶へ形成する。
SQLiteへの承認済み記憶保存とoutbox作成を同一transactionで行う。Chroma登録失敗時は本文を
別ファイルへ退避せず、outboxの`memory_id`でSQLiteの承認済み記憶を再読して冪等に再試行する。

長期記憶の訂正はSQLite正本の更新、失効は`expires_at`／状態による取得除外、ユーザー削除は
`approved_memories`行のhard deleteとして区別する。hard deleteでは`character_id`と`memory_id`を
持つmetadata-onlyの`DELETE` outboxを同一transactionで作成し、削除済みSQLite本文を再読せず
Chromaから冪等に削除する。SQLite commit後の同じ削除操作でChroma deleteを同期試行し、
失敗時はoutbox retryで回復する。さらに定期reconciliationでSQLiteに存在しないChroma orphanを
削除し、欠落entryとmetadata不一致をSQLite正本から修復する。

検索時はChromaの結果をそのままpromptへ渡さず、`memory_id`をSQLiteで引き直し、
`character_id`、状態、TTL、policy versionを確認する。さらにSQLiteの`normalized_text`へ
共通の決定論的絶対禁止scannerを再適用し、検出した記憶をpromptへ渡さない。

current user queryに絶対禁止finding、意味分類の`SENSITIVE`／`ABSTAIN`、または判定障害がある場合は
RAG検索自体をskipし、RAGなしで会話を続ける。検索順位は意味的関連度を主とし、関連度が同等の
候補間だけ`last_user_mentioned_at`をtie-breakに使う。検索やassistantの言及では同日時を更新しない。

詳細な不変条件とMVP境界は
`docs/decisions/rag-memory-privacy-policy-2026-07.md`および
`docs/decisions/wave2-memory-formation-retrieval-2026-08.md`を参照する。

promptへcontextを供給する境界は次に分ける。

```text
ContextProvider
├─ ConversationHistoryProvider
├─ PersonaMemoryProvider
└─ AddonRecordProvider
```

初期domain provider候補:

- `core`: 人格記憶
- `temporary:agriculture`: addon完成前の農業記録
- `temporary:recipe`: addon完成前のレシピ記録

記憶は人格ごとに分離できるようにする。

```text
characters/
└─ miori/
   ├─ miori.card.json  # runtime人格定義のSource of Truth
   ├─ personality.md   # 人格設計の編集資料（runtimeでは未使用）
   ├─ world.md
   └─ memory-policy.md  # 方針本文と実装設定への案内
```

現行の記憶・記録モデルは`docs/decisions/wave2-memory-formation-retrieval-2026-08.md`、
RAG privacyの不変条件は`docs/decisions/rag-memory-privacy-policy-2026-07.md`で管理する。
`docs/decisions/archive/miori-memory-policy-2026-06.md`は初期検討の履歴ADRとして保持する。
`backend/app/memory/memory_policy.json`は認識語彙・pattern・閾値・追加禁止設定の実行時Source of
Truthとするが、ADRとtyped policy schemaが定める絶対禁止を削除・許可へ反転できない。
