# Characters

このディレクトリでは、`digital-souls` で扱うAI人格ごとの設定を管理する。

各人格は、単なるプロンプトではなく、人格・世界観・記憶方針・将来の実装参照元をまとめた「デジタル人格」として扱う。

## 目的

`characters/` は、複数のAI人格を将来的に追加できるようにするためのルートディレクトリである。

人格ごとの設定を分離することで、以下を実現する。

- 人格ごとの性格・話し方・世界観を独立して管理する
- 記憶方針や応答方針を人格ごとに切り替えられるようにする
- 将来的なRAG、エージェント制御、UI表現との連携時に参照しやすくする
- 配信AI、パーソナルAI、生活支援AIなど用途ごとの差分を管理しやすくする

## ディレクトリ構成

```text
characters/
└─ miori/
   ├─ miori.card.json
   ├─ personality.md
   ├─ world.md
   └─ memory-policy.md
```

## 人格一覧

| ID | 名前 | 役割 | 状態 |
|---|---|---|---|
| miori | 光織 | 昼と夜の境目に佇むAI / 初期パーソナルAI人格 | 草案完了 |

## 各人格ディレクトリの基本構成

各人格ディレクトリでは、原則として以下のファイルを配置する。

| ファイル | 役割 |
|---|---|
| `{id}.card.json` | runtimeで使用する人格Core・条件付きCharacter Lore・表示名・会話例・応答指示の正本 |
| `personality.md` | Character Cardを編集するときの非runtime補助資料 |
| `world.md` | 世界観、比喩体系、用語、演出方針、応答上の扱い |
| `memory-policy.md` | 記憶方針本文の移動先と実装設定ファイルへの案内 |

## Character Card V3の主要フィールド

`{id}.card.json`では、常に応答へ反映するCharacter Coreと、条件に応じて参照する
Character Loreを分けて管理する。主要フィールドの役割は次のとおり。

| 項目 | 意味 |
|---|---|
| `description` | キャラクターの概要と役割。「誰で、何をするAIか」を定義する |
| `personality` | 性格、口調、感情表現、回答の長さなど、常に維持する振る舞いを定義する |
| `scenario` | ユーザーとの関係、利用状況、どのように支援するかを定義する |
| `system_prompt` | 最優先の応答規則。事実性、安全性、Character Loreの扱い、簡潔さなどを指示する |
| `first_mes` | 履歴がない場合に表示する最初のassistant発話。通常の会話promptには挿入しない |
| `mes_example` | 基準となる会話例。具体的な口調や回答形式をモデルへ示す |
| `post_history_instructions` | 会話履歴の後、現在のユーザー発言の直前に置く省略可能な最終指示。記憶や保存操作など、現在発話の近くで適用する規則に使う |
| `character_book` | 質問や状況に応じて選択する静的な公開世界設定をCharacter Loreとして定義する |

必要に応じて、将来的に以下のようなファイルを追加する。

| ファイル | 役割 |
|---|---|
| `design.md` | 外見デザイン、衣装、キーアイテム、Live2D/VRM向け注意点 |
| `voice.md` | 声質、話速、口調、音声合成向け方針 |
| `prompts.md` | システムプロンプトや用途別プロンプトの草案 |
| `scenarios.md` | 配信、日常会話、農業日誌、レシピ管理などの応答例 |

## 命名規則

人格ディレクトリ名は、英字小文字のIDを使用する。

例:

```text
characters/miori/
characters/example-character/
```

日本語名や表示名は、各人格の `{id}.card.json` の `data.name` に記載する。

## 新しい人格を追加する場合

1. `characters/{id}/` ディレクトリを作成する
2. 基本構成の4ファイルを作成する
3. このREADMEの「人格一覧」テーブルに追記する
4. `docs/decisions/` に設計経緯を残す

## 管理方針

- runtime人格設定はCharacter Cardで管理し、通常の会話ログや一時メモとは分ける
- 条件付きの世界観・用語・状況設定はCharacter Card V3の`data.character_book`で管理する
- 人格の核となる設定は、安易に削除・上書きしない
- 変更する場合は、理由が分かるようにコミットメッセージやIssueに残す
- 実装コードから参照する場合は、人格IDをキーとして扱う
- RAGや長期記憶へ人格設定を取り込まず、runtimeではCharacter Cardを直接参照する
- Character Bookは静的な人格設定としてRAGと分離し、Entry本文やkeyをapplication logへ記録しない

## 初期人格

初期人格は `miori` とする。

光織は、昼と夜の境目に佇むAI / パーソナルAI人格であり、農業日誌、レシピ管理、メモ管理、配信補助、長期的なパーソナルAI用途を想定する。

詳細は以下を参照。

- `characters/miori/miori.card.json`
- `characters/miori/personality.md`（非runtimeの編集補助）
- `characters/miori/world.md`
- `docs/decisions/wave2-memory-formation-retrieval-2026-08.md`
- `docs/decisions/archive/miori-memory-policy-2026-06.md`（初期検討の履歴ADR）
- `backend/app/memory/memory_policy.json`
