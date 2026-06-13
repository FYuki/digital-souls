## 技術スタック

| 層 | 技術 | 備考 |
|---|---|---|
| コアフレームワーク | AIRI（候補） | Phase 3 で検証 |
| LLM (small) | Ollama + Gemma 4B / Qwen 8B / Llama 8B | Mac mini（常時稼働予定） |
| LLM (medium) | Ollama + 大型モデル | WindowsメインPC（RTX搭載） |
| LLM (large) | Cloud GPU / VM | Windows未起動時フォールバック |
| 記憶 | RAG（Qdrant） + PostgreSQL | Phase 3〜 |
| アバター（標準） | Live2D | VTube Studio / OBS連携 |
| アバター（配信） | VRM | 3tene / Warudo / VNyan 等 |
| 音声 | 未定 | Phase 5〜 |
| UI | 静止画UI / チャットUI / Discord Bot / Web UI | 用途別 |

## 環境

- OS: Windows 11
- WSL2 Ubuntu: メイン開発環境（Ollama / Docker）
- WindowsメインPC: RTX搭載、重いAI処理・配信処理専用（必要時のみ起動）
- Mac mini（将来）: 常時稼働サーバー（軽量LLM・記憶DB・生活支援ツール）
- Cloud GPU/VM: WindowsメインPC未起動時のフォールバック

## 現在の開発フェーズ

| Phase | 状態 | 内容 |
|---|---|---|
| Phase 0 | ✅ 完了 | 方針整理・リポジトリ構成 |
| Phase 1 | 📝 草案完了 | 光織の人格・世界観・記憶方針（`characters/miori/` に格納） |
| Phase 2 | ⬜ 次 | WSL2 開発環境・Docker・Ollama 検証 |
| Phase 3 | ⬜ 未着手 | コア基盤検証（AIRI / 推論ルーター / RAG） |
| Phase 4 | ⬜ 未着手 | パーソナルAI機能（農業日誌・レシピ・メモ） |
| Phase 5 | ⬜ 未着手 | 表現・配信連携（Live2D / VRM） |
| Phase 6 | ⬜ 未着手 | 常時稼働化（Mac mini 本番運用） |

> **注意**: Phase 2 に入るまでは実装コードを追加しない。現在はドキュメント中心の開発フェーズ。

## 規約

- **ドキュメントはすべて日本語で記述する**（コードのコメントも日本語を基本とする）
- コミットメッセージは Conventional Commits 形式（英語可）

## リポジトリ構成

```
digital-souls/
├─ docs/
│  ├─ roadmap.md               # 開発ロードマップ
│  ├─ system-architecture.md   # システムアーキテクチャ
│  ├─ infrastructure-policy.md # インフラ方針
│  ├─ development-environment.md
│  ├─ repository-policy.md
│  └─ decisions/               # 検討経緯・意思決定ログ
├─ characters/
│  └─ miori/                   # 初期人格「光織」
│     ├─ personality.md        # 人格設定・話し方
│     ├─ world.md              # 世界観・比喩体系
│     └─ memory-policy.md      # 記憶方針・プライバシー
└─ src/                        # 将来の実装コード（Phase 2〜）
```

## 実装フロー管理（TAKT）

このプロジェクトは **[TAKT](https://github.com/nrslib/takt)** を使って実装フローを管理する。

```bash
# インストール（初回のみ）
npm install -g takt

# タスクを AI と相談して積む
takt

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
