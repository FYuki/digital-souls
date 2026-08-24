## 技術スタック

> **2026-06-17 方針転換**: AIRIフォーク利用を取りやめ、自作BE（FastAPI）+ 自作FE（Vite + Svelte）構成に移行した。
> 理由・経緯は `docs/decisions/` を参照。

| 層 | 技術 | 備考 |
|---|---|---|
| バックエンド | FastAPI（Python） | キャラクター管理・LLM振り分け・WebSocket・RAGを実装 |
| フロントエンド | Vite + Svelte + TypeScript | テキストチャットUI → 音声UIへ拡張 |
| LLM (small) | Ollama + gemma4:e4b | ローカル開発・常用想定 |
| LLM (medium/large) | Claude / GPT（クラウド） | LLM振り分けルーターの枠のみ用意、初期はダミー |
| 長期記憶 | RAG（Chroma + nomic-embed-text） + SQLite | キャラクターごとに完全独立。`character_id`を全レコードに付与。詳細は `docs/decisions/wave2-memory-formation-retrieval-2026-08.md` |
| 音声通信（現行） | WebSocket | Wave 3移行前のターン形式とbaseline計測に使用 |
| 音声通信（Wave 3） | LiveKit | 採用済み。AudioTransport境界でConversation Coreから分離する |
| STT | Whisper（faster-whisper, WSL2ローカル） | |
| TTS | VOICEVOX（WSL2ローカル） | 日本語対応 |
| アバター（標準） | Live2D | VTube Studio / OBS連携、Phase 7〜 |
| アバター（配信） | VRM | 3tene / Warudo / VNyan 等、Phase 7〜 |
| UI | ブラウザ → Discord → スマホ → デスクトップ | MVPはブラウザのみ |

## 環境

- OS: Windows 11
- WSL2 Ubuntu-dev: メイン開発環境（TAKT / FastAPI / Vite / テスト。データは破棄可能）
- WSL2 Ubuntu-dogfood（#50で構築）: 運用相当の安定環境（実会話履歴 / Ollama / VOICEVOX）
- WindowsメインPC: RTX搭載、重いAI処理・配信処理専用（必要時のみ起動）
- Mac mini（将来）: 常時稼働サーバー（M4 Pro 48GB想定。軽量LLM・Whisper・記憶DB・生活支援ツール）
- Cloud GPU/VM: WindowsメインPC未起動時のフォールバック

開発とdogfoodの分離、データ保持、Wave 2との依存順は
`docs/decisions/local-dogfood-environment-2026-08.md`を参照する。

## 現在の開発フェーズ

`docs/roadmap.md` を参照。

## 規約

- **ドキュメントはすべて日本語で記述する**（コードのコメントも日本語を基本とする）
- コミットメッセージは Conventional Commits 形式（英語可）
- TAKT上のチャット（タスク起票・相談）では対話的な質問ツールが使用できないため、ユーザーに確認したい事項はテキストで質問を表示する

## リポジトリ構成

```
digital-souls/
├─ docs/
│  ├─ roadmap.md               # 開発ロードマップ
│  ├─ system-architecture.md   # システムアーキテクチャ
│  ├─ infrastructure-policy.md # インフラ方針
│  ├─ development-environment.md
│  ├─ repository-policy.md
│  ├─ testing-policy.md        # テスト方針（ユニット/インテグレーション/E2Eの区別、外部サービス実接続要件）
│  └─ decisions/               # 検討経緯・意思決定ログ
├─ characters/
│  └─ miori/                   # 初期人格「光織」
│     ├─ personality.md        # 人格設定・話し方
│     ├─ world.md              # 世界観・比喩体系
│     └─ memory-policy.md      # 記憶方針・プライバシー
├─ backend/                    # 自作BE（FastAPI）
└─ frontend/                   # 自作FE（Vite + Svelte）
```

## 実装フロー管理（TAKT）

このプロジェクトは **[TAKT](https://github.com/nrslib/takt)** を使って実装フローを管理する。

```bash
# インストール（初回のみ）
npm install -g takt

# タスクを AI と相談して積む
takt

# GitHub issueをタスクとして積む（#を含む場合はクオート必須）
takt add "#9"

# 積んだタスクを実行
takt run

# タスク一覧・結果確認
takt list
```

### ワークフロー

| ワークフロー | 用途 |
|---|---|
| `default`（ビルトイン） | 標準開発（計画→実装→レビュー） |
| `default-mini`（ビルトイン） | 小規模な変更・ホットフィックス |

### TAKT ディレクトリ

```
.takt/
├─ config.yaml     # プロジェクト設定
└─ workflows/      # カスタムワークフロー（必要時）
```

`tasks/`・`logs/` は `.takt/.gitignore` で除外済み（ローカル管理）。
