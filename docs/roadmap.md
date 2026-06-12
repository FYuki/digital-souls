# 開発ロードマップ

## 目的

`digital-souls` の開発を、人格設計・基盤設計・インフラ整備・配信連携の順に段階的に進める。

## Phase 0: 方針整理

- リポジトリ構成を決める
- GitHub運用方針を決める
- AIRIをコア候補として採用する前提で調査する
- Live2D / VRM / 静止画UIの役割を整理する
- Mac mini / Windows / Cloud VMの役割を整理する

## Phase 1: 人格設計

- 美織の人格設定を作成する
- 美織の世界観を作成する
- 記憶方針を定義する
- 複数人格対応を前提に `characters/` 構成を整える

## Phase 2: 開発環境整備

- Windows + WSL2で開発環境を構築する
- Docker利用方針を決める
- Ollamaで軽量LLMを検証する
- Gemma 4B級モデルの応答品質と速度を検証する
- 将来のMac mini移行手順を整理する

## Phase 3: コア基盤検証

- AIRIを人格・記憶・エージェント制御のコア候補として検証する
- 推論ルーターを設計する
- ローカルLLM、WindowsメインPC、Cloud VMを切り替える設計を検討する
- 長期記憶・RAG・ツール実行の構成を検討する

## Phase 4: パーソナルAI機能

- 農業日誌ツールを設計する
- アレンジレシピ管理ツールを設計する
- メモ・タスク・日常ログ管理の導入を検討する
- Web UIまたはチャットUIを検討する

## Phase 5: 表現・配信連携

- 基本の姿としてLive2Dを採用する
- パーソナルAI用途では静止画UIも許容する
- 配信時のみVRM利用を検討する
- VTube Studio、OBS、3tene、Warudo、VNyanなどの連携を検討する

## Phase 6: 常時稼働化

- Mac miniを常時稼働サーバーとして導入する
- 軽量LLMをMac miniで常用する
- 重い処理はWindowsメインPCへ委譲する
- Windows未起動時はCloud GPU/VMへ委譲する
- バックアップ、監視、復旧手順を整備する
