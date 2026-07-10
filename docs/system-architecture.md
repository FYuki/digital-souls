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
* `memory/` — 長期記憶（RAG）基盤。`chroma_store.py`（ベクトルDB）、`conversation_log.py`（SQLite会話ログ）、`embedder.py`（埋め込み生成）、`memory_policy.py`（`backend/app/memory/memory_policy.json` を参照する保存・参照ルール判定）、`rag_service.py`（検索・合成の統合サービス）
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

光織の記憶方針本文は `docs/decisions/miori-memory-policy-2026-06.md`、実装が参照する機械可読な設定値は `backend/app/memory/memory_policy.json` で管理する。
