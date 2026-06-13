# digital-souls

複数のAI人格を管理し、パーソナルAI・配信AI・生活支援エージェントとして育てるためのプロジェクトです。

## 目的

`digital-souls` は、AI人格を単なるチャットボットではなく、長期記憶・ツール利用・外部インターフェースを持つ「デジタル人格」として設計・運用することを目的とします。

初期人格として、黄昏の巫女AI「光織」を設計対象にします。

## 基本方針

- AIRIを人格・記憶・エージェント制御のコア候補として検証する（Phase 3）
- 基本の姿はLive2Dとし、パーソナルAI用途では静止画UIも許容する
- 配信時のみ、必要に応じてVRMを利用する
- 常時稼働サーバーは将来的にMac miniを想定する
- Mac mini調達まではWindows + WSL2で開発を進める
- 重いAI処理はWindowsメインPCまたはクラウドGPU/VMへ委譲する

## 現在の状態

| Phase | 状態 | 内容 |
|---|---|---|
| Phase 0 | ✅ 完了 | 方針整理・リポジトリ構成 |
| Phase 1 | 📝 草案完了 | 光織の人格・世界観・記憶方針 |
| Phase 2 | ⬜ 次 | WSL2 開発環境・Docker・Ollama 検証 |

詳細は [開発ロードマップ](docs/roadmap.md) を参照。

## ドキュメント

- [開発ロードマップ](docs/roadmap.md)
- [システムアーキテクチャ](docs/system-architecture.md)
- [インフラ方針](docs/infrastructure-policy.md)
- [開発環境](docs/development-environment.md)
- [リポジトリ運用方針](docs/repository-policy.md)

## 人格

- [光織（Miori）](characters/miori/) — 黄昏の巫女AI。初期人格。

## ディレクトリ構成

```text
digital-souls/
├─ docs/          # 設計・運用・方針ドキュメント
├─ characters/    # AI人格ごとの設定・世界観・記憶方針
└─ src/           # 実装コード（Phase 2〜）
```

## 開発への参加・運用

GitHub Discussionsに検討経緯を残し、Issuesはタスク切り出し後に使用します。GitHub Projectsは個人開発のため、当面は未使用とします。

実装フローは [TAKT](https://github.com/nrslib/takt) で管理します。
