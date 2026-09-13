# リポジトリ運用方針

## 基本方針

`digital-souls`は、複数のAIキャラクターとその実行基盤を管理するリポジトリとして扱う。
光織専用リポジトリにはせず、キャラクターごとの定義・会話・記憶の境界を保つ。

## 格納方針

```text
repo/
├─ README.md                  # 概要・現在の実装範囲・起動と文書の入口
├─ AGENTS.md                  # エージェント向けの開発規約
├─ docs/
│  ├─ glossary.md             # 用語・実装名・定義の参照先
│  ├─ roadmap.md
│  ├─ system-architecture.md
│  ├─ infrastructure-policy.md
│  ├─ development-environment.md
│  ├─ testing-policy.md
│  ├─ repository-policy.md
│  └─ decisions/
│     └─ <topic>-YYYY-MM.md
├─ characters/
│  └─ miori/
│     ├─ miori.card.json       # runtime定義の正本
│     ├─ personality.md        # Character Card編集用の非runtime補助資料
│     ├─ world.md
│     └─ memory-policy.md      # 方針本文と実装設定への案内
├─ backend/                   # FastAPI
├─ frontend/                  # Vite + Svelte + TypeScript
├─ contracts/                 # FE/BE共有契約
├─ environments/              # Profile・環境管理
├─ infra/                     # Compose・構築・運用
├─ scripts/                   # 起動・検証・運用入口
└─ whisper_service/           # 共有STT service
```

## 文書ごとの責務

同じ内容を複数文書へ詳細に複製せず、概要・定義・判断・現在の挙動・操作を分ける。

| 文書 | 責務 |
|---|---|
| `README.md` | プロジェクトの目的、mainにある実装範囲、未接続・任意有効化の境界、起動と詳細文書の入口 |
| `AGENTS.md` | 開発規約、作業時に参照する正本、環境・検証・文書更新上の注意 |
| `glossary.md` | 日本語・英語・実装名の対応、意味と混同しやすい境界、実装／一部実装／設計の区別、定義の参照先 |
| `roadmap.md` | Phase／Wave等で実現する目標。個別Issueの進捗・実装条件・schema versionを重複管理しない |
| `enhancement-plan.md` | 機能の分解、依存関係、実施順序、完了イメージ |
| `system-architecture.md` | 現在のコードで実現している構成、責務境界、runtimeの挙動を現在形で説明。将来方針は明示的に分離 |
| `decisions/` | 判断の背景、選択肢、決定事項、不変条件。タスクの進捗は管理しない |
| GitHub Issues | 具体的な実装・文書作業の範囲、依存、テスト、完了条件。完了後は当時の実装契約の記録 |
| 運用手順書 | 起動・停止、backup、migration、rollback、障害対応等の実行手順 |
| 受入記録・test evidence | 実行した環境・版・条件・結果・残課題。現在の機能説明とは分け、過去の実行結果を上書きしない |

schemaやデータ保持の方針はADR、現在のschemaとruntime挙動はコードと`system-architecture.md`、対応作業はIssueへ記載する。
ADRの`ACTIVE`は有効な設計判断を表すだけであり、実装完了・既定有効・dogfood受入済みを意味しない。

### 文書・用語の更新

新規設計と既存実装の変更のどちらでも、PR作成前に次を確認する。

1. 新しい概念やAPI／schema上の用語、既存語の意味・所有境界を追加・変更した場合は、`glossary.md`の定義・実装名・状態・参照先を更新する。設計段階で登録した語は、実装・接続時にも見直す。
2. 利用可能な機能、標準の起動・設定、主要ディレクトリや責務が変わった場合は、README、AGENTS、Backend／Frontend README、アーキテクチャ、該当する運用手順を照合する。
3. 実装済み、一部実装、設計のみを混ぜない。対象ブランチのコード・設定・受入条件を確認し、未マージの変更をmainの機能として説明しない。

用語集は定義への入口であり、新たな仕様判断の正本にはしない。新しい判断はADRへ、残作業・依存はIssueへ残す。
設計支援でADRを作成した場合も、既存実装の用語補完や関連する現行文書の更新を省略しない。
文書のみのPRでは、相対リンク・参照する実装名・Markdownの確認結果と、アプリケーションテストの実施有無・理由を記録する。

## characters

`characters/`はキャラクターごとにディレクトリを分ける。

基本ファイルは次のとおり。

- `{id}.card.json`：runtimeで使用する人格定義・表示名・会話例・応答指示の正本。
- `personality.md`、`world.md`：Character Card編集用の非runtime補助資料。
- `memory-policy.md`：方針本文と実装設定への案内。

runtimeはCardを直接読み込み、`personality.md`から人格情報を合成しない。必要に応じて`voice.md`、`appearance.md`等の補助資料を置いてよい。
キャラクターの固定設定と、経験によって変化する人格・興味・記憶を混同しない。

## docs/decisions

`docs/decisions/`には検討経緯と意思決定ログを残す。
現行ADRは直下、完全に置換・失効したADRは`docs/decisions/archive/`へ配置する。
状態タグ、部分改定時の優先関係、archive運用は[ADR案内](decisions/README.md)を参照する。
archive内の文書を現行仕様や実装の正本として使用しない。

ファイル命名規則は`<topic>-YYYY-MM.md`とする。用語を含むADRと用語集を相互に辿れるようにする。

## GitHub Issues・Discussions・Projects

Issuesは実装またはドキュメント作業を具体的なタスクに切り出した後に使用する。
初期の検討はDiscussionsに残し、実行可能な作業になったものをIssue化する。
個人開発のため、GitHub Projectsは当面使用せず、必要になった場合に導入する。

## テスト証跡

テスト件数・通過数をレポートへ記載する場合は、実行ログを一次証跡として扱う。
`mypy`等の対象ファイル件数はソース追加で変動するため、契約テストに固定しない。成功可否と可変件数を分ける。

テストの層分け、外部サービスへの実接続要件、命名規則は[テスト方針](testing-policy.md)を参照する。
実連携を完了条件として報告する場合は、その実接続テストの実行ログを根拠とする。
モックE2Eの成功を実接続の成功へ読み替えず、既存の過去ログを今回の再実行結果として報告しない。

## ブランチ運用

ブランチは`main`、epicブランチ、作業ブランチの3層で運用する。

```text
作業ブランチ ──PR──> epicブランチ ──PR──> main ──明示的なdeploy──> dogfood
```

| 種別 | 役割 | 作成元 | PRのマージ先 |
|---|---|---|---|
| `main` | dogfoodへ投入可能な品質を満たすブランチ | - | - |
| epicブランチ | 1つのepicに含まれる変更をまとめ、epic単位で受け入れる統合ブランチ | `main` | `main` |
| 作業ブランチ | 実装、修正、ドキュメント更新等を作業・Issue単位で行うブランチ | 対象のepicブランチ | 対象のepicブランチ |

### 運用手順

1. epic開始時に`main`からepicブランチを作成する。
2. 作業・Issueごとにepicブランチから作業ブランチを作成する。
3. 作業ブランチの変更はepicブランチ向けPRでレビュー・マージする。
4. epicの変更と受入条件が揃ったら、epicブランチから`main`へのPRでレビュー・マージする。
5. mainへマージされたcommitをdogfoodへの投入候補とする。マージだけでは自動deployせず、所定の手順で明示的にdeployする。

作業ブランチから`main`へ直接PRを作成しない。epicブランチは`epic/*`、作業ブランチは変更内容に応じて`feature/*`、`fix/*`、`docs/*`、`infra/*`、`character/*`等の接頭辞を使用する。
