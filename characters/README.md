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

各人格ディレクトリでは、以下のファイルを配置する。

| ファイル | 役割 |
|---|---|
| `{id}.card.json` | runtimeが読み込むCharacter Card V3。人格、表示名、会話例、応答指示、初回メッセージ、TTS設定の正本 |
| `personality.md` | 人格設計の検討資料。runtimeからは読み込まない |
| `world.md` | 世界観設計の検討資料。runtimeからは読み込まない |
| `memory-policy.md` | 記憶方針本文の移動先と実装設定ファイルへの案内 |

必要に応じて、将来的に以下のようなファイルを追加する。

| ファイル | 役割 |
|---|---|
| `design.md` | 外見デザイン、衣装、キーアイテム、Live2D/VRM向け注意点 |
| `voice.md` | 声質、話速、口調、音声合成向け方針 |
| `prompts.md` | Character Cardへ反映する前の用途別プロンプトの草案 |
| `scenarios.md` | 配信、日常会話、農業日誌、レシピ管理などの応答例 |

## 命名規則

人格ディレクトリ名は、英字小文字のIDを使用する。

例:

```text
characters/miori/
characters/example-character/
```

日本語名や表示名は、各人格のCharacter Card V3にある`data.name`を正本とする。

## 新しい人格を追加する場合

1. `characters/{id}/` ディレクトリを作成する
2. `{id}.card.json`を`spec: "chara_card_v3"`、`spec_version: "3.0"`で作成する
3. `data`にV3の必須フィールドをすべて設定する
4. 音声設定を`data.extensions.digital_souls.tts_config`へ設定する
5. 設計資料として`personality.md`、`world.md`、`memory-policy.md`を作成する
6. このREADMEの「人格一覧」テーブルに追記する
7. `docs/decisions/` に設計経緯を残す

Character Cardの`data`では、次のフィールドを使用する。

| フィールド | 役割 |
|---|---|
| `name` | UI等で使う表示名の正本 |
| `description` | キャラクター概要 |
| `personality` | 性格、話し方 |
| `scenario` | ユーザーとの関係、世界観 |
| `system_prompt` | 常に守る応答方針 |
| `first_mes` | 履歴がない場合の初回assistant表示 |
| `mes_example` | 会話および話し方の例 |
| `post_history_instructions` | 現在のユーザー発言より後に適用する最終指示 |
| `creator`、`character_version`、`creator_notes` | 作成者、人格版、作成者向け注記 |
| `alternate_greetings`、`group_only_greetings` | 代替・グループ向け初回メッセージ |
| `extensions` | 実装固有設定のnamespace |

VOICEVOXを使う場合は、次の構造で設定する。

```json
{
  "extensions": {
    "digital_souls": {
      "tts_config": {
        "engine": "voicevox",
        "speaker_id": 14
      }
    }
  }
}
```

## 管理方針

- 人格設定は、通常の会話ログや一時メモとは分けて管理する
- 人格の核となる設定は、安易に削除・上書きしない
- 変更する場合は、理由が分かるようにコミットメッセージやIssueに残す
- 実装コードから参照する場合は、人格IDをキーとしてCharacter Cardを読み込む
- RAGや長期記憶に取り込む場合は、人格設定ファイルを通常ログより高い優先度で扱う

## 初期人格

初期人格は `miori` とする。

光織は、昼と夜の境目に佇むAI / パーソナルAI人格であり、農業日誌、レシピ管理、メモ管理、配信補助、長期的なパーソナルAI用途を想定する。

詳細は以下を参照。

- `characters/miori/personality.md`
- `characters/miori/world.md`
- `docs/decisions/miori-memory-policy-2026-06.md`
- `backend/app/memory/memory_policy.json`
