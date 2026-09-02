# 光織 Character Card 編集補助

光織のruntime人格定義の正本は
[`miori.card.json`](./miori.card.json) である。このファイルはruntimeから読み込まず、
Character Cardを編集するときの光織固有の確認事項だけを記録する。

Character Card V3の各フィールドに共通する役割は
[`characters/README.md`](../README.md#character-card-v3の主要フィールド)を参照する。

## 編集時の確認事項

- 人格に関する情報は、このファイルへ複製せずCharacter Cardの対応フィールドを更新する。
- 常に必要な人格と応答方針はCharacter Coreへ置き、条件付きの公開世界設定は
  `character_book`へ分離する。
- `world.md`等の構想資料とruntime定義が異なる場合は、Character Cardを正とする。
- `world.md`の要素をCharacter Cardへ反映するときはそのまま複製せず、世界観表現が通常会話で
  強くなりすぎないよう、Coreまたは条件付きCharacter Loreに合わせて調整する。
- 長期記憶の内容はRAGから挿入し、Character Cardには必要な場合の扱い方だけを定義する。
- `first_mes` は通常のpromptへ挿入されないため、毎ターン必要な指示を書かない。
- `post_history_instructions` は省略可能であり、指定する場合は現在発話の直前に置く必要がある
  最終指示だけを書く。
- 記憶の保存可否そのものは `memory-policy.md` と実装設定を正本とし、人格表現と混在させない。
- 外見資料を追加する場合は `design.md`、音声資料を追加する場合は `voice.md` に分離する。
