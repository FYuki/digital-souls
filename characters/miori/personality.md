# 光織 Character Card 編集補助

光織のruntime人格定義の正本は
[`miori.card.json`](./miori.card.json) である。このファイルはruntimeから読み込まず、
Character Cardを編集するときの役割分担と確認事項だけを記録する。

## フィールドの役割

| フィールド | 記載する内容 |
|---|---|
| `description` | キャラクターの概要、由来、外見上の識別情報 |
| `personality` | 性格、感情の傾向、話し方、価値観、好み |
| `scenario` | ユーザーとの関係、世界観、用途、状況ごとの振る舞い |
| `system_prompt` | 必ず守る応答方針、操作時の確認、安全上の制約 |
| `first_mes` | 履歴がない場合だけ表示する最初のassistant発話 |
| `mes_example` | 口調と応答方針を示す会話例 |
| `post_history_instructions` | 履歴と現在発話の後に適用する最終指示 |

## 編集時の確認事項

- 人格に関する情報は、このファイルへ複製せずCharacter Cardの対応フィールドを更新する。
- `first_mes` は通常のpromptへ挿入されないため、毎ターン必要な指示を書かない。
- `post_history_instructions` は省略可能であり、指定する場合は現在発話の後に置く指示だけを書く。
- 記憶の保存可否そのものは `memory-policy.md` と実装設定を正本とし、人格表現と混在させない。
- 外見資料を追加する場合は `design.md`、音声資料を追加する場合は `voice.md` に分離する。
