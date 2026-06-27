# 開発ロードマップ

## 目的

`digital-souls` の開発を、人格設計・基盤実装・音声対応・長期記憶・配信連携の順に段階的に進める。

> **2026-06-17 方針転換**: AIRIフォーク利用を取りやめ、自作BE（FastAPI）+ 自作FE（Vite + Svelte）構成に移行した。
> 理由・経緯は `docs/decisions/` を参照。

## Phase 0: 方針整理

[x] リポジトリ構成を決める
[x] GitHub運用方針を決める
[x] AIRIをコア候補として調査する（→ 自作方針に転換、フォークなし）
[x] Live2D / VRM / 静止画UIの役割を整理する
[x] Mac mini / Windows / Cloud VMの役割を整理する

## Phase 1: 人格設計

[x] 光織の人格設定を作成する
[x] 光織の世界観を作成する
[x] 記憶方針を定義する
[x] 複数人格対応を前提に `characters/` 構成を整える

## Phase 2: 開発環境整備

[x] Windows + WSL2で開発環境を構築する
[x] Docker利用方針を決める
[x] Ollamaで軽量LLMを検証する
[x] Gemma 4B級モデルの応答品質と速度を検証する
[x] 将来のMac mini移行手順を整理する

## Phase 3: テキストチャット基盤（自作BE/FE）

[x] リポジトリ構成を自作BE/FE用に整備する（backend/, frontend/）
[x] FastAPI プロジェクト基盤を構築する（Ollama接続・キャラクターロード）
[x] Vite + Svelte テキストチャットUIを実装する
[x] キャラクター指定方式（リクエストパラメータ・ステートレス）を実装する

## Phase 4: 音声対応

[x] BEにWebSocketエンドポイントを追加する
[x] STT（Whisper）+ TTS（VOICEVOX）パイプラインを実装する
[ ] FEにマイク入力（VAD対応）・音声再生UIを実装する（テキスト/音声をWS接続に統合、#14）
[ ] BEのWS音声フレームに処理中キュー（1件保持・上書き）を実装する（#29）
[ ] WebSocketの遅延を計測し、LiveKit移行の必要性を判断する

## Phase 5: 長期記憶（RAG）

[x] RAG（Chroma + nomic-embed-text）のMVP構成を実装する（`docs/decisions/Multi-character-db-2026-06.md` の決定事項に基づく）
[ ] 全レコードに `character_id` を付与したスキーマで会話ログ（SQLite）と長期記憶（Chroma）を連携する
[ ] キャラクターごとに完全独立したメモリ空間を構成する（DB共有はしない）
[ ] 光織の記憶方針（`docs/decisions/miori-memory-policy-2026-06.md`）と実装設定（`backend/app/memory/memory_policy.json`）に沿った保存・参照ルールを反映する
[ ] Embeddingモデル・チャンク戦略を調整する

## Phase 6: パーソナルAI機能

[ ] 農業日誌ツールを設計する
[ ] アレンジレシピ管理ツールを設計する
[ ] メモ・タスク・日常ログ管理の導入を検討する
[ ] Web UIまたはチャットUIを検討する

## Phase 7: 表現・配信連携

[ ] 基本の姿としてLive2Dを採用する
[ ] パーソナルAI用途では静止画UIも許容する
[ ] 配信時のみVRM利用を検討する
[ ] VTube Studio、OBS、3tene、Warudo、VNyanなどの連携を検討する

## Phase 8: 常時稼働化・マルチクライアント対応

[ ] Mac miniを常時稼働サーバーとして導入する（M4 Pro 48GB想定）
[ ] 埋め込みモデル・LLM推論・VOICEVOXを共有サーバー化し、各キャラクターはAPI経由で呼び出す
[ ] 複数キャラクター同時発話時のGPU推論キューイングを設計する（運用上のボトルネックがDB層より先に出る想定）
[ ] 重い処理はWindowsメインPCへ委譲する
[ ] Windows未起動時はCloud GPU/VMへ委譲する
[ ] Discord Bot / スマホアプリ / デスクトップアプリへの展開を検討する
[ ] バックアップ、監視、復旧手順を整備する
[ ] 運用規模が拡大した場合（5体超・同時アクセス増）にQdrant/PostgreSQLへの移行を再検討する
