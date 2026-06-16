# 開発ロードマップ

## 目的

`digital-souls` の開発を、人格設計・基盤設計・インフラ整備・配信連携の順に段階的に進める。

## Phase 0: 方針整理

[x] リポジトリ構成を決める
[x] GitHub運用方針を決める
[x] AIRIをコア候補として採用する前提で調査する
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

## Phase 3: コア基盤検証

[ ] AIRIをサイドカーとして特定バージョンで導入する（upstream参照、フォークなし）
[ ] AIRIのserver-runtimeをWSL2上で起動し、動作確認する
[ ] Character Card（光織）をAIRIにインポートし、人格反映を確認する
[ ] gemma3:4b（Ollama）とAIRIのWebSocket接続を確認する
[ ] 推論ルーターを設計する（ローカル / Windows / Cloud VMの切り替え）
[ ] 長期記憶・RAG・ツール実行の構成を検討する（memory-pgvectorの評価含む）
[ ] プラグイン開発方針を確定する（plugin-sdkの接続方式を検証）

### 備忘：上流へのコントリビューション候補

- YouTube Live Chat対応（AIRIのv0.9ロードマップ Issue #840 に記載あり、services/youtube-bot として実装するパターン）
- VOICEVOX音声対応（AIRIではなくUnspeechリポジトリへ。PR #887 が失敗した反省点が明確なため通る可能性あり）

## Phase 4: パーソナルAI機能

[ ] 農業日誌ツールを設計する
[ ] アレンジレシピ管理ツールを設計する
[ ] メモ・タスク・日常ログ管理の導入を検討する
[ ] Web UIまたはチャットUIを検討する

## Phase 5: 表現・配信連携

[ ] 基本の姿としてLive2Dを採用する
[ ] パーソナルAI用途では静止画UIも許容する
[ ] 配信時のみVRM利用を検討する
[ ] VTube Studio、OBS、3tene、Warudo、VNyanなどの連携を検討する

## Phase 6: 常時稼働化

[ ] Mac miniを常時稼働サーバーとして導入する
[ ] 軽量LLMをMac miniで常用する
[ ] 重い処理はWindowsメインPCへ委譲する
[ ] Windows未起動時はCloud GPU/VMへ委譲する
[ ] バックアップ、監視、復旧手順を整備する
