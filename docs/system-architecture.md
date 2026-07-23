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
* `characters/loader.py` — `characters/` 配下の人格定義（personality.md・card.json等）のロード
* `llm/` — LLM振り分けルーター（`router.py`）とクライアント実装。`ollama_client.py`（ローカルOllama、常用）、`base.py`（クライアント共通インターフェース）。クラウドLLM（Claude等）向けクライアントは未実装のスタブ
* `memory/` — 会話履歴と長期記憶の基盤。SQLiteに同一conversation再開用の履歴と承認済み長期記憶を責務分離して保存し、Chromaは承認済み長期記憶だけの派生検索インデックスとして扱う。`memory_policy.py`は`backend/app/memory/memory_policy.json`の認識設定と、アプリケーションの非緩和policyを組み合わせて保存先別に判定する
* `stt/whisper_client.py` — faster-whisperによる音声認識
* `tts/voicevox_client.py` / `tts/speech_synthesizer.py` — VOICEVOXによる音声合成
* `audio/transport.py` / `audio_pipeline.py` — 音声フレームの送受信・パイプライン制御

### フロントエンド（Vite + Svelte, `frontend/src/`）

* `lib/audio/transport.ts` — WebSocket通信を抽象化する `AudioTransport`（将来のLiveKit移行に備えたシーム）
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

```text
受信した会話
  ├─ 現在ターンの応答生成（原文は処理中だけ利用）
  └─ 共通privacy scanner / 意味assessment
       ├─ 履歴用policy
       │    ├─ MASK / STORE
       │    └─ SKIP_CONTENT
       │         └─ SQLite: conversations / conversation_turns
       └─ RAG admission policy
            └─ ALLOW_STRUCTURED
                 └─ SQLite: approved_memories + memory_index_outbox
                      └─ Chroma: 承認済み記憶の派生index
```

共通privacy scannerは保存先を決めず、カテゴリ、本文中の位置、reason code、versionを
型付きfindingとして処理中だけ返す。履歴用policyは、APIキー、password、秘密鍵、決済認証、
口座番号、政府ID、私用連絡先、正確な住所等の値をマスクし、明示的な履歴非保存要求または
安全にマスクできない場合は本文を破棄する。health、心理状態、金融状況、第三者情報等の話題は
同一conversationの履歴として保持できるが、MVPではRAG長期記憶へ昇格させない。
userとassistantの双方へ同じscannerとsanitizerを適用し、原文、検出値、マスク前本文を
SQLiteやapplication logへ残さない。

RAG長期記憶はpositive allowlist方式とし、許可型へ正規化され、機微情報検査と必要なユーザー確認を
通過した`ApprovedMemoryCandidate`だけをSQLiteへ保存する。SQLiteを正本、Chromaを派生indexとし、
SQLiteへの承認済み記憶保存とoutbox作成を同一transactionで行う。Chroma登録失敗時は本文を
別ファイルへ退避せず、outboxの`memory_id`でSQLiteの承認済み記憶を再読して冪等に再試行する。

検索時はChromaの結果をそのままpromptへ渡さず、`memory_id`をSQLiteで引き直し、
`character_id`、状態、TTL、policy versionを確認する。さらにSQLiteの`normalized_text`へ
共通の決定論的絶対禁止scannerを再適用し、検出した記憶をpromptへ渡さない。

詳細な不変条件とMVP境界は
`docs/decisions/rag-memory-privacy-policy-2026-07.md`を参照する。

初期ツール候補:

* 農業日誌
* アレンジレシピ管理
* メモ管理
* タスク管理
* 配信ログ
* キャラクター記憶

記憶は人格ごとに分離できるようにする。

```text
characters/
└─ miori/
   ├─ personality.md
   ├─ world.md
   └─ memory-policy.md  # 方針本文と実装設定への案内
```

光織の記憶方針本文は`docs/decisions/miori-memory-policy-2026-06.md`、RAG privacyの不変条件は
`docs/decisions/rag-memory-privacy-policy-2026-07.md`で管理する。
`backend/app/memory/memory_policy.json`は認識語彙・pattern・閾値・追加禁止設定の実行時Source of
Truthとするが、ADRとtyped policy schemaが定める絶対禁止を削除・許可へ反転できない。
